#!/usr/bin/env python3
"""
Field Nation Content Auditor
=============================
Crawls support.fieldnation.com and/or fieldnation.com for specific keywords/phrases
in both page text and embedded images (OCR). Saves flagged URLs to a new Google Doc
and a local CSV.

SETUP (one-time):
  1. Install dependencies:
       pip3 install -r requirements.txt
       python3 -m playwright install chromium

  2. (Optional) Install Tesseract for image text (screenshots inside articles):
       brew install tesseract

  3. Google Doc output — one-time credential setup:
       a. Go to https://console.cloud.google.com
       b. Create/select a project
       c. Enable "Google Docs API" and "Google Drive API"
       d. Credentials → Create OAuth 2.0 Client ID (Desktop app)
       e. Download JSON → rename to credentials.json → place next to audit.py
       On first run a browser window will open for sign-in; token is cached after that.

USAGE:
  # Search help center (support.fieldnation.com) — default
  python3 audit.py "mark complete"
  python3 audit.py "mark complete" "mark the work order complete"

  # Include the marketing site too
  python3 audit.py --site both "marketplace"

  # Marketing site only
  python3 audit.py --site marketing "provider quality assurance policy"

  # Skip image OCR (faster)
  python3 audit.py --no-ocr "mark complete"

  # Skip Google Doc, save CSV only
  python3 audit.py --no-gdoc "mark complete"
"""

import argparse
import csv
import io
import logging
import os
import pickle
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Page, Browser, TimeoutError as PlaywrightTimeout

# Google API
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False

# OCR
try:
    from PIL import Image
    import pytesseract
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────

SITES = {
    "support":   "https://support.fieldnation.com",
    "marketing": "https://www.fieldnation.com",
}

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]

CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), "credentials.json")
TOKEN_FILE       = os.path.join(os.path.dirname(__file__), "token.pickle")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}

PAGE_LOAD_WAIT  = "networkidle"  # wait for JS to settle
PAGE_TIMEOUT_MS = 25_000         # 25s per page
CRAWL_DELAY_S   = 0.5            # polite delay between pages
MAX_PAGES       = 800            # safety cap (BFS may find more than sitemap)

# URL path segments that indicate non-content pages -- skip during BFS
_SKIP_PATTERNS = re.compile(
    r"/(login|logout|register|profile|account|signin|signout"
    r"|oauth|auth/|callback|api/|wp-admin|wp-login|search\b)",
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── URL Discovery ─────────────────────────────────────────────────────────────

def _fetch_xml(url: str, session: requests.Session) -> Optional[ET.Element]:
    try:
        r = session.get(url, timeout=12)
        r.raise_for_status()
        return ET.fromstring(r.content)
    except Exception as exc:
        log.debug(f"XML fetch failed {url}: {exc}")
        return None


def get_sitemap_urls(base_url: str, session: requests.Session) -> List[str]:
    """Collect all page URLs from sitemap(s). Returns empty list if none found."""
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    collected: List[str] = []
    seen_sitemaps: Set[str] = set()

    def parse_sm(sm_url: str, depth: int = 0) -> None:
        if depth > 4 or sm_url in seen_sitemaps:
            return
        seen_sitemaps.add(sm_url)
        root = _fetch_xml(sm_url, session)
        if root is None:
            return
        # Sitemap index
        for child in root.findall(".//sm:sitemap/sm:loc", ns):
            parse_sm(child.text.strip(), depth + 1)
        # URL set
        for u in root.findall(".//sm:url/sm:loc", ns):
            collected.append(u.text.strip())

    # Check robots.txt for Sitemap: directives
    try:
        robots = session.get(f"{base_url}/robots.txt", timeout=10).text
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                parse_sm(line.split(":", 1)[1].strip())
    except Exception:
        pass

    if not collected:
        for candidate in [f"{base_url}/sitemap.xml", f"{base_url}/sitemap_index.xml"]:
            parse_sm(candidate)
            if collected:
                break

    # Deduplicate
    return list(dict.fromkeys(collected))


def crawl_site_urls(base_url: str, browser: Browser, max_pages: int = MAX_PAGES) -> List[str]:
    """Fallback: BFS crawl with JS rendering to find all pages."""
    parsed_base = urlparse(base_url)
    visited: Set[str] = set()
    queue: List[str] = [base_url]
    found: List[str] = []

    page = browser.new_page()
    try:
        while queue and len(visited) < max_pages:
            url = queue.pop(0).split("#")[0].rstrip("/")
            if not url or url in visited:
                continue
            visited.add(url)

            try:
                page.goto(url, wait_until=PAGE_LOAD_WAIT, timeout=PAGE_TIMEOUT_MS)
                found.append(url)

                for a in page.query_selector_all("a[href]"):
                    href = (a.get_attribute("href") or "").split("#")[0].strip()
                    if not href:
                        continue
                    full = urljoin(url, href).rstrip("/")
                    if urlparse(full).netloc == parsed_base.netloc and full not in visited:
                        queue.append(full)

                time.sleep(CRAWL_DELAY_S)
            except Exception as exc:
                log.debug(f"Crawl error {url}: {exc}")
    finally:
        page.close()

    return found


def discover_urls(site_key: str, session: requests.Session, browser: Browser) -> List[str]:
    base = SITES[site_key]
    log.info(f"Discovering URLs for {base} ...")

    urls = get_sitemap_urls(base, session)
    if urls:
        log.info(f"  Sitemap: {len(urls)} URLs found")
    else:
        log.info("  No sitemap found — crawling with headless browser...")
        urls = crawl_site_urls(base, browser)
        log.info(f"  Crawl: {len(urls)} URLs found")

    # Keep only same-domain pages
    parsed_base = urlparse(base)
    urls = [u for u in dict.fromkeys(urls) if urlparse(u).netloc == parsed_base.netloc]
    return urls


# ── Page Content Extraction ───────────────────────────────────────────────────

# JavaScript that collects ALL <a href> values including those inside
# Salesforce Lightning Web Component (LWC) Shadow DOM trees.
# Standard query_selector_all("a[href]") is blind to shadow DOM.
_COLLECT_LINKS_JS = """
() => {
    const hrefs = [];
    const seen  = new Set();
    function collect(root) {
        if (!root) return;
        try {
            const anchors = root.querySelectorAll('a[href]');
            for (const a of anchors) {
                const h = a.getAttribute('href');
                if (h && !seen.has(h)) { seen.add(h); hrefs.push(h); }
            }
            // Recurse into every shadow root on this level
            const all = root.querySelectorAll('*');
            for (const el of all) {
                if (el.shadowRoot) collect(el.shadowRoot);
            }
        } catch(e) {}
    }
    collect(document.body);
    return hrefs;
}
"""


def _normalize_url(url: str) -> str:
    """Strip query-string and fragment; used for BFS deduplication."""
    p = urlparse(url)
    return p._replace(query="", fragment="").geturl().rstrip("/")


def _should_follow(url: str, base_netloc: str) -> bool:
    """Return False for URLs that are clearly not article content pages."""
    p = urlparse(url)
    if p.netloc != base_netloc:
        return False
    if p.scheme not in ("http", "https"):
        return False
    if _SKIP_PATTERNS.search(p.path):
        return False
    return True


def get_page_content(
    url: str,
    page: Page,
    base_netloc: Optional[str] = None,
) -> Tuple[str, str, List[str], List[str]]:
    """
    Render page with Playwright and return
    (title, visible_text, img_urls, same-domain links).
    Links are normalised (query strings stripped) for BFS deduplication.
    Pass base_netloc to enable link collection; omit to skip it.
    """
    try:
        page.goto(url, wait_until=PAGE_LOAD_WAIT, timeout=PAGE_TIMEOUT_MS)

        # Title: prefer article <h1> over generic <title>
        title = page.title() or ""
        if not title or title.lower() in ("field nation", "support central", ""):
            h1 = page.query_selector("h1")
            if h1:
                title = h1.inner_text().strip()
        if not title:
            title = url

        text = page.inner_text("body") or ""

        # Collect image srcs
        img_urls: List[str] = []
        for img in page.query_selector_all("img[src]"):
            src = img.get_attribute("src") or ""
            if src and not src.startswith("data:"):
                img_urls.append(urljoin(url, src))

        # Collect same-domain links for BFS using shadow-DOM-aware JS.
        # Salesforce LWC renders navigation inside shadow roots, which
        # query_selector_all("a[href]") cannot see.
        links: List[str] = []
        if base_netloc:
            try:
                raw_hrefs: List[str] = page.evaluate(_COLLECT_LINKS_JS) or []
            except Exception:
                raw_hrefs = []

            for href in raw_hrefs:
                href = href.strip()
                if not href or href.startswith("mailto:") or href.startswith("tel:"):
                    continue
                full = _normalize_url(urljoin(url, href.split("#")[0]))
                if _should_follow(full, base_netloc):
                    links.append(full)

        return title, text, img_urls, links

    except PlaywrightTimeout:
        log.debug(f"Timeout loading {url}")
        return url, "", [], []
    except Exception as exc:
        log.debug(f"Page error {url}: {exc}")
        return url, "", [], []


def get_page_text_and_images(url: str, page: Page) -> Tuple[str, str, List[str]]:
    """Backward-compatible wrapper -- returns (title, text, img_urls)."""
    title, text, img_urls, _ = get_page_content(url, page)
    return title, text, img_urls


# ── Term Matching ─────────────────────────────────────────────────────────────

# Words stripped from search terms before proximity matching so they don't
# block a match when the page uses slightly different phrasing.
_STOP_WORDS = frozenset({
    "the", "a", "an", "as", "of", "in", "on", "at", "to", "for",
    "is", "are", "was", "were", "be", "been", "by", "that", "this",
    "it", "its", "or", "and", "but", "not", "with", "from",
})

# Max words that may appear between the first and last key word of a term.
# 15 comfortably covers "mark [the work order as] complete" (5 gap words).
_PROXIMITY = 15


def _word_matches(key: str, token: str) -> bool:
    """
    Return True if *token* is a word-form variation of *key*.

    Handles common English suffixes so that, e.g., key="mark" matches
    "marked", "marking", "marks", and key="complete" matches "completed",
    "completing", "completely".  Does NOT match unrelated words that merely
    share a prefix (e.g. key="mark" will not match "market").
    """
    if token == key:
        return True
    for suffix in ("ed", "ing", "er", "es", "ly", "d", "s"):
        if token.endswith(suffix):
            stem = token[: -len(suffix)]
            # Direct match after stripping suffix
            if stem == key:
                return True
            # e-drop: "completing" → stem "complet" → key "complete"
            if stem + "e" == key:
                return True
            # Doubled-consonant: "stopping" → stem "stopp" → key "stop"
            if len(stem) >= 2 and stem[-1] == stem[-2] and stem[:-1] == key:
                return True
    return False


def find_terms_in_text(text: str, terms: List[str]) -> Tuple[List[str], List[str]]:
    """
    Case-insensitive search with automatic variation handling.

    For each term:
      1. Fast path  — exact substring match (no false positives).
      2. Variation path — strips stop words from the term, then scans a
         sliding window of _PROXIMITY tokens looking for word-form matches
         of every remaining key word.  This means the user only needs to
         enter "mark complete" and the tool will also find "marked complete",
         "mark the work order complete", "mark the work order as complete", etc.

    Returns (matched_terms, context_snippets).
    """
    text_lower  = text.lower()
    # Tokenise into words for proximity matching (preserves original for snippets)
    raw_tokens: List[str] = re.findall(r"[A-Za-z''-]+", text)
    lower_tokens: List[str] = [t.lower() for t in raw_tokens]

    matched: List[str] = []
    snippets: List[str] = []

    for term in terms:
        # ── 1. Exact phrase match (fast path) ──────────────────────────────
        idx = text_lower.find(term.lower())
        if idx != -1:
            start = max(0, idx - 70)
            end   = min(len(text), idx + len(term) + 100)
            raw   = re.sub(r"\s+", " ", text[start:end]).strip()
            matched.append(term)
            snippets.append(f"...{raw}...")
            continue

        # ── 2. Proximity + word-form match (variation path) ────────────────
        term_words = term.lower().split()
        key_words  = [w for w in term_words if w not in _STOP_WORDS] or term_words

        found_at = -1
        for i, tok in enumerate(lower_tokens):
            if not _word_matches(key_words[0], tok):
                continue
            # First key word matched — check remaining keys within window
            window = lower_tokens[i : i + _PROXIMITY]
            if all(any(_word_matches(kw, t) for t in window) for kw in key_words[1:]):
                found_at = i
                break

        if found_at >= 0:
            matched.append(term)
            snip_start = max(0, found_at - 5)
            snip_end   = min(len(raw_tokens), found_at + _PROXIMITY)
            snippets.append(f"...{' '.join(raw_tokens[snip_start:snip_end])}...")

    return matched, snippets


def find_terms_in_images(
    image_urls: List[str],
    terms: List[str],
    session: requests.Session,
) -> Tuple[List[str], List[str]]:
    """OCR each image and search for terms. Returns (matched_terms, image_notes)."""
    if not OCR_AVAILABLE or not image_urls:
        return [], []

    matched: List[str] = []
    notes:   List[str] = []

    for img_url in image_urls:
        try:
            r = session.get(img_url, timeout=10)
            if r.status_code != 200:
                continue
            pil_img  = Image.open(io.BytesIO(r.content)).convert("RGB")
            ocr_text = pytesseract.image_to_string(pil_img).lower()
            for term in terms:
                if term.lower() in ocr_text and term not in matched:
                    matched.append(term)
                    notes.append(f"[found in screenshot: {img_url}]")
        except Exception as exc:
            log.debug(f"OCR error {img_url}: {exc}")

    return matched, notes


# ── Audit Orchestrator ────────────────────────────────────────────────────────

def run_audit_bfs(
    sites: List[str],
    search_terms: List[str],
    use_ocr: bool = True,
    on_event=None,
    cancel_event=None,
    extra_seeds: Optional[List[str]] = None,
) -> List[Dict]:
    """
    BFS audit -- discovers URLs as a side-effect of scanning.

    Starts from the sitemap (or homepage) and follows every link found on each
    page, so articles not listed in the sitemap are still visited.

    extra_seeds: additional URLs to always include (e.g. articles not linked
    from any navigation page). These are added to the queue before scanning
    starts, and links discovered FROM them expand the queue further.

    on_event(dict) is called for progress; events emitted:
      {"type": "discovering", "site": url}
      {"type": "started",     "total": n, "site": url}   -- initial seed count
      {"type": "scanning",    "current": n, "total": n, "url": url}
      {"type": "flagged",     "result": {...}}

    Returns list of flagged-page dicts:
      url, title, site, matched_terms, snippets, match_types
    """
    def emit(event: dict) -> None:
        if on_event:
            on_event(event)

    session = requests.Session()
    session.headers.update(HEADERS)
    all_results: List[Dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page    = browser.new_page()

        try:
            for site_key in sites:
                base         = SITES[site_key]
                parsed_base  = urlparse(base)
                base_netloc  = parsed_base.netloc

                emit({"type": "discovering", "site": base})
                log.info(f"Discovering URLs for {base} (sitemap + BFS link-follow)...")

                # Seed queue from sitemap (fast HTTP, no browser needed)
                sitemap_urls = get_sitemap_urls(base, session)
                seed = [_normalize_url(u) for u in sitemap_urls] if sitemap_urls else [base]
                log.info(f"  Sitemap seed: {len(seed)} URLs")

                queue:   List[str] = list(seed)
                queued:  Set[str]  = set(queue)
                visited: Set[str]  = set()
                scanned = 0

                # Inject any user-specified URLs that might not be in the sitemap
                # or reachable via navigation links (e.g. orphaned articles).
                if extra_seeds:
                    added_extra = 0
                    for raw in extra_seeds:
                        norm = _normalize_url(raw.strip())
                        if norm and norm not in queued and _should_follow(norm, base_netloc):
                            queue.append(norm)
                            queued.add(norm)
                            added_extra += 1
                    if added_extra:
                        log.info(f"  Extra seeds: +{added_extra} user-specified URLs added to queue")

                emit({"type": "started", "total": len(queue), "site": base})

                while queue:
                    if cancel_event and cancel_event.is_set():
                        break
                    if len(visited) >= MAX_PAGES:
                        log.warning(f"MAX_PAGES ({MAX_PAGES}) reached for {base}")
                        break

                    url = queue.pop(0)
                    if url in visited:
                        continue
                    visited.add(url)
                    scanned += 1

                    total_est = scanned + len(queue)
                    emit({"type": "scanning", "current": scanned,
                          "total": total_est, "url": url})

                    if scanned % 25 == 0:
                        log.info(f"  [{scanned}/~{total_est}] BFS scanning... "
                                 f"({len(queue)} in queue)")

                    title, text, img_urls, new_links = get_page_content(
                        url, page, base_netloc=base_netloc
                    )

                    # Expand queue with freshly discovered links
                    added = 0
                    for link in new_links:
                        if link not in queued:
                            queue.append(link)
                            queued.add(link)
                            added += 1
                    if added:
                        log.debug(f"  +{added} new links discovered from {url}")

                    if not text:
                        time.sleep(CRAWL_DELAY_S)
                        continue

                    text_matched, text_snippets = find_terms_in_text(text, search_terms)

                    img_matched, img_notes = [], []
                    if use_ocr and OCR_AVAILABLE:
                        img_matched, img_notes = find_terms_in_images(
                            img_urls, search_terms, session
                        )

                    all_matched = list(dict.fromkeys(text_matched + img_matched))
                    if not all_matched:
                        time.sleep(CRAWL_DELAY_S)
                        continue

                    match_types = []
                    if text_matched: match_types.append("text")
                    if img_matched:  match_types.append("image/OCR")

                    result = {
                        "url":           url,
                        "title":         title,
                        "site":          site_key,
                        "matched_terms": all_matched,
                        "snippets":      text_snippets + img_notes,
                        "match_types":   ", ".join(match_types),
                    }
                    all_results.append(result)
                    emit({"type": "flagged", "result": result})
                    log.info(f"  FLAGGED: {title[:60]}  →  {all_matched}")

                    time.sleep(CRAWL_DELAY_S)

        finally:
            page.close()
            browser.close()

    return all_results


def run_audit(
    sites: List[str],
    search_terms: List[str],
    use_ocr: bool = True,
    extra_seeds: Optional[List[str]] = None,
) -> List[Dict]:
    """CLI wrapper around run_audit_bfs (kept for backward compatibility)."""
    def on_event(event):
        if event.get("type") == "started":
            log.info(f"Starting BFS scan (seed: {event['total']} URLs from sitemap) ...")
        elif event.get("type") == "scanning":
            cur   = event.get("current", 0)
            total = event.get("total", 0)
            if cur % 25 == 0 or cur == 1:
                log.info(f"  [{cur}/~{total}] scanning...")
        elif event.get("type") == "flagged":
            pass  # already logged inside run_audit_bfs

    return run_audit_bfs(sites, search_terms, use_ocr, on_event=on_event,
                         extra_seeds=extra_seeds)


# ── Output: CSV ───────────────────────────────────────────────────────────────

def save_csv(flagged: List[Dict], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["URL", "Page Title", "Site", "Matched Terms", "Match Type", "Snippets"])
        for r in flagged:
            w.writerow([
                r["url"],
                r["title"],
                r["site"],
                "; ".join(r["matched_terms"]),
                r["match_types"],
                " | ".join(r["snippets"]),
            ])
    log.info(f"CSV saved → {path}")


# ── Output: Google Doc ────────────────────────────────────────────────────────

def get_google_credentials() -> Optional[object]:
    if not GOOGLE_AVAILABLE:
        log.warning("google-api-python-client not installed — skipping Google Doc.")
        return None

    if not os.path.exists(CREDENTIALS_FILE):
        log.warning(
            f"\n{'─'*60}\n"
            "credentials.json not found. To enable Google Doc output:\n"
            "  1. https://console.cloud.google.com → select/create project\n"
            "  2. APIs & Services → Enable 'Google Docs API' + 'Google Drive API'\n"
            "  3. Credentials → Create OAuth 2.0 Client ID (Desktop app)\n"
            "  4. Download JSON → rename to credentials.json → place in fn-content-auditor/\n"
            "Results are still saved to CSV.\n"
            f"{'─'*60}\n"
        )
        return None

    creds = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow  = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GOOGLE_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)

    return creds


def _build_doc_requests(
    flagged: List[Dict],
    search_terms: List[str],
    sites: List[str],
    ocr_used: bool,
) -> List[Dict]:
    """Produce Google Docs API batchUpdate requests for the full document."""

    now        = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    site_names = [SITES[s] for s in sites]

    # ── Build plain text body ──
    lines: List[Tuple[str, str]] = []   # (style, text)

    lines.append(("heading1", "Field Nation Content Audit"))
    lines.append(("normal",   f"Date: {now}"))
    lines.append(("normal",   f"Search terms: {', '.join(repr(t) for t in search_terms)}"))
    lines.append(("normal",   f"Sites audited: {', '.join(site_names)}"))
    lines.append(("normal",   f"Image OCR: {'enabled' if ocr_used else 'disabled'}"))
    lines.append(("normal",   ""))

    if not flagged:
        lines.append(("heading2", "No matches found"))
        lines.append(("normal",   "None of the search terms were found on any audited pages."))
    else:
        lines.append(("heading2", f"Pages Flagged for Review ({len(flagged)})"))
        lines.append(("normal",   ""))

        for r in flagged:
            lines.append(("heading3", r["title"]))
            lines.append(("url",      r["url"]))
            lines.append(("normal",   f"Matched: {', '.join(r['matched_terms'])}   |   Via: {r['match_types']}"))
            for snippet in r["snippets"]:
                lines.append(("snippet", snippet))
            lines.append(("normal", ""))

    # ── Assemble full text string & record positions ──
    full_text  = ""
    seg_map: List[Tuple[int, int, str, str]] = []   # (start, end, style, raw_text)

    for style, text in lines:
        start     = len(full_text)
        full_text += text + "\n"
        seg_map.append((start, len(full_text), style, text))

    # ── Build API requests ──
    reqs: List[Dict] = []
    offset = 1  # Google Docs body starts at index 1

    # Insert all text at once
    reqs.append({"insertText": {"location": {"index": 1}, "text": full_text}})

    # Apply styles
    for start, end, style, raw in seg_map:
        s = start + offset
        e = end   + offset

        if style == "heading1":
            reqs.append({"updateParagraphStyle": {
                "range": {"startIndex": s, "endIndex": e},
                "paragraphStyle": {"namedStyleType": "HEADING_1"},
                "fields": "namedStyleType",
            }})
        elif style == "heading2":
            reqs.append({"updateParagraphStyle": {
                "range": {"startIndex": s, "endIndex": e},
                "paragraphStyle": {"namedStyleType": "HEADING_2"},
                "fields": "namedStyleType",
            }})
        elif style == "heading3":
            reqs.append({"updateParagraphStyle": {
                "range": {"startIndex": s, "endIndex": e},
                "paragraphStyle": {"namedStyleType": "HEADING_3"},
                "fields": "namedStyleType",
            }})
        elif style == "url" and raw:
            reqs.append({"updateTextStyle": {
                "range": {"startIndex": s, "endIndex": e - 1},   # exclude newline
                "textStyle": {
                    "foregroundColor": {"color": {"rgbColor": {"red": 0.07, "green": 0.36, "blue": 0.73}}},
                    "link": {"url": raw},
                },
                "fields": "foregroundColor,link",
            }})
        elif style == "snippet":
            reqs.append({"updateTextStyle": {
                "range": {"startIndex": s, "endIndex": e - 1},
                "textStyle": {
                    "italic": True,
                    "foregroundColor": {"color": {"rgbColor": {"red": 0.4, "green": 0.4, "blue": 0.4}}},
                },
                "fields": "italic,foregroundColor",
            }})

    return reqs


def create_google_doc(
    flagged: List[Dict],
    search_terms: List[str],
    sites: List[str],
    ocr_used: bool,
    creds,
) -> Optional[str]:
    try:
        svc   = build("docs", "v1", credentials=creds)
        title = (
            f"FN Content Audit — {', '.join(search_terms)} "
            f"— {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        doc    = svc.documents().create(body={"title": title}).execute()
        doc_id = doc["documentId"]

        reqs = _build_doc_requests(flagged, search_terms, sites, ocr_used)
        svc.documents().batchUpdate(documentId=doc_id, body={"requests": reqs}).execute()

        url = f"https://docs.google.com/document/d/{doc_id}"
        log.info(f"Google Doc created → {url}")
        return url

    except Exception as exc:
        log.error(f"Google Doc creation failed: {exc}")
        return None


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit Field Nation websites for keywords in text and images.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="See docstring at top of file for full setup and usage.",
    )
    parser.add_argument(
        "terms",
        nargs="+",
        metavar="TERM",
        help='One or more search terms, e.g. "mark complete" "submit for review"',
    )
    parser.add_argument(
        "--site",
        choices=["support", "marketing", "both"],
        default="support",
        help="Which site to audit (default: support)",
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="Skip image OCR (faster, text-only)",
    )
    parser.add_argument(
        "--no-gdoc",
        action="store_true",
        help="Skip Google Doc, save CSV only",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        default=None,
        help="CSV output path (default: auto-named in script directory)",
    )

    args  = parser.parse_args()
    sites = ["support", "marketing"] if args.site == "both" else [args.site]
    use_ocr = not args.no_ocr

    if use_ocr and not OCR_AVAILABLE:
        log.warning(
            "pytesseract/Pillow not fully available — image OCR disabled. "
            "Install Tesseract (brew install tesseract) to enable."
        )
        use_ocr = False

    # ── Run ──
    flagged = run_audit(sites, args.terms, use_ocr=use_ocr)

    log.info(f"\nAudit complete — {len(flagged)} page(s) flagged.")

    # ── CSV ──
    if args.output:
        csv_path = args.output
    else:
        slug     = re.sub(r"[^\w]+", "_", args.terms[0])[:30]
        ts       = datetime.now().strftime("%Y%m%d_%H%M")
        csv_path = os.path.join(os.path.dirname(__file__), f"audit_{slug}_{ts}.csv")
    save_csv(flagged, csv_path)

    # ── Google Doc ──
    gdoc_url = None
    if not args.no_gdoc:
        creds = get_google_credentials()
        if creds:
            gdoc_url = create_google_doc(flagged, args.terms, sites, use_ocr, creds)

    # ── Terminal summary ──
    print(f"\n{'='*65}")
    print("FIELD NATION CONTENT AUDIT — RESULTS")
    print(f"Terms:    {', '.join(repr(t) for t in args.terms)}")
    print(f"Sites:    {', '.join(SITES[s] for s in sites)}")
    print(f"Flagged:  {len(flagged)} page(s)")
    print(f"{'='*65}")
    for r in flagged:
        print(f"\n  {r['url']}")
        print(f"  Title:   {r['title'][:80]}")
        print(f"  Matched: {', '.join(r['matched_terms'])}  [{r['match_types']}]")
        if r["snippets"]:
            print(f"  Context: {r['snippets'][0][:130]}")
    print(f"\nCSV:      {csv_path}")
    if gdoc_url:
        print(f"Doc:      {gdoc_url}")
    print()


if __name__ == "__main__":
    main()
