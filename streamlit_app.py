"""
Field Nation Content Auditor — Streamlit App
=============================================
Run locally:  streamlit run streamlit_app.py
Deploy:       Push to GitHub → connect at share.streamlit.io
"""

import os
import subprocess
import sys
import time
from datetime import datetime

import pandas as pd
import streamlit as st

# ── Install Playwright browser on first run (needed on Streamlit Cloud) ───────
@st.cache_resource(show_spinner="Setting up browser (first run only)…")
def _install_browser():
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True,
    )

_install_browser()

# ── Import audit utilities ────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
import audit as _audit

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="FN Content Auditor",
    page_icon="🔍",
    layout="wide",
)

st.markdown("""
<style>
  .block-container { max-width: 1100px; padding-top: 2rem; }
  div[data-testid="stHorizontalBlock"] { align-items: center; }
  .term-chip { display:inline-block; background:#E8F0FD; color:#0062E0;
    border-radius:20px; padding:3px 12px; margin:3px; font-size:13px; }
</style>
""", unsafe_allow_html=True)

# ── Header ────────────────────────────────────────────────────────────────────
st.title("Field Nation Content Auditor")
st.caption(
    "Searches **support.fieldnation.com** and/or **fieldnation.com** for keywords. "
    "Verb forms and filler words are matched automatically — searching "
    "**\"mark complete\"** also finds *\"marked complete\"* and "
    "*\"mark the work order as complete\"*."
)
st.divider()

# ── Session state defaults ────────────────────────────────────────────────────
if "terms" not in st.session_state:
    st.session_state.terms = []

# ── Search Terms ──────────────────────────────────────────────────────────────
st.subheader("Search Terms")

add_col, btn_col = st.columns([5, 1])
with add_col:
    new_term = st.text_input(
        "term",
        placeholder='Type a term and press Enter or click Add…',
        label_visibility="collapsed",
        key="new_term_input",
    )
with btn_col:
    add_clicked = st.button("Add", use_container_width=True)

if add_clicked or (new_term and st.session_state.get("_last_term") != new_term):
    term = new_term.strip()
    if term and term not in st.session_state.terms:
        st.session_state.terms.append(term)
        st.session_state["_last_term"] = term
        st.rerun()

# Display current terms
if st.session_state.terms:
    cols = st.columns(min(len(st.session_state.terms), 6))
    for i, term in enumerate(st.session_state.terms):
        with cols[i % 6]:
            if st.button(f"✕  {term}", key=f"rm_{i}", use_container_width=True):
                st.session_state.terms.pop(i)
                st.rerun()
    st.caption("Click a term to remove it.")
else:
    st.info("No search terms yet — add at least one above.")

st.divider()

# ── Settings ──────────────────────────────────────────────────────────────────
set_col1, set_col2 = st.columns(2)

with set_col1:
    st.subheader("Sites to Audit")
    site_choice = st.radio(
        "site",
        options=[
            "Help Center  (support.fieldnation.com)",
            "Marketing Site  (fieldnation.com)",
            "Both Sites",
        ],
        label_visibility="collapsed",
    )

with set_col2:
    st.subheader("Options")
    if _audit.OCR_AVAILABLE:
        use_ocr = st.checkbox("Enable image OCR — searches text inside screenshots (slower)")
    else:
        st.checkbox(
            "Enable image OCR *(install Tesseract to enable)*",
            value=False, disabled=True,
        )
        use_ocr = False

site_map = {
    "Help Center  (support.fieldnation.com)":  ["support"],
    "Marketing Site  (fieldnation.com)":        ["marketing"],
    "Both Sites":                               ["support", "marketing"],
}
sites = site_map[site_choice]

st.divider()

# ── Run Audit ─────────────────────────────────────────────────────────────────
run_disabled = not st.session_state.terms
st.button(
    "▶  Run Audit",
    type="primary",
    disabled=run_disabled,
    key="run_btn",
    help="Add at least one search term to enable." if run_disabled else None,
)

if st.session_state.get("run_btn"):
    terms = st.session_state.terms[:]

    import requests as _req
    from playwright.sync_api import sync_playwright

    # ── UI placeholders ──
    status_box   = st.empty()
    progress_bar = st.empty()
    url_caption  = st.empty()
    results_box  = st.empty()

    status_box.info("Discovering pages…")
    results: list[dict] = []

    try:
        session = _req.Session()
        session.headers.update(_audit.HEADERS)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page    = browser.new_page()

            try:
                all_url_pairs: list[tuple] = []
                for site_key in sites:
                    url_caption.caption(f"Fetching sitemap for {_audit.SITES[site_key]}…")
                    urls = _audit.discover_urls(site_key, session, browser)
                    for url in urls:
                        all_url_pairs.append((url, site_key))

                total = len(all_url_pairs)
                status_box.info(f"Scanning **0 / {total}** pages…")

                for i, (url, site_key) in enumerate(all_url_pairs, 1):
                    pct = i / total
                    progress_bar.progress(
                        pct,
                        text=f"{i} / {total} pages  ·  {len(results)} flagged",
                    )
                    url_caption.caption(url)

                    title, text, img_urls = _audit.get_page_text_and_images(url, page)
                    if not text:
                        time.sleep(_audit.CRAWL_DELAY_S)
                        continue

                    text_matched, text_snippets = _audit.find_terms_in_text(text, terms)

                    img_matched, img_notes = [], []
                    if use_ocr and _audit.OCR_AVAILABLE:
                        img_matched, img_notes = _audit.find_terms_in_images(
                            img_urls, terms, session
                        )

                    all_matched = list(dict.fromkeys(text_matched + img_matched))
                    if not all_matched:
                        time.sleep(_audit.CRAWL_DELAY_S)
                        continue

                    match_types = []
                    if text_matched: match_types.append("text")
                    if img_matched:  match_types.append("image/OCR")

                    results.append({
                        "URL":          url,
                        "Page Title":   title,
                        "Site":         site_key,
                        "Matched Terms": "; ".join(all_matched),
                        "Found Via":    ", ".join(match_types),
                        "Context":      (text_snippets + img_notes)[0]
                                        if (text_snippets + img_notes) else "",
                    })

                    # Update live table on every new flag
                    results_box.dataframe(
                        pd.DataFrame(results),
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "URL": st.column_config.LinkColumn("URL", display_text="Open ↗"),
                        },
                    )

                    status_box.info(
                        f"Scanning **{i} / {total}** pages  ·  "
                        f"**{len(results)}** flagged so far…"
                    )
                    time.sleep(_audit.CRAWL_DELAY_S)

            finally:
                page.close()
                browser.close()

    except Exception as exc:
        st.error(f"Audit error: {exc}")
        st.stop()

    # ── Finished ──
    url_caption.empty()
    progress_bar.progress(1.0, text="Done")
    status_box.success(f"Audit complete — **{len(results)}** page(s) flagged.")

    if results:
        df  = pd.DataFrame(results)
        slug = terms[0].replace(" ", "_")[:30]
        date = datetime.now().strftime("%Y%m%d")

        results_box.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "URL": st.column_config.LinkColumn("URL", display_text="Open ↗"),
            },
        )

        st.download_button(
            label="⬇  Export CSV",
            data=df.to_csv(index=False).encode("utf-8"),
            file_name=f"fn_audit_{slug}_{date}.csv",
            mime="text/csv",
            type="primary",
        )
    else:
        results_box.info("No pages matched the search terms.")
