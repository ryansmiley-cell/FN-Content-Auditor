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

    # ── UI placeholders ──
    status_box   = st.empty()
    progress_bar = st.empty()
    url_caption  = st.empty()
    results_box  = st.empty()

    status_box.info("Discovering pages and starting BFS scan…")
    results: list[dict] = []

    # Mutable state shared with the on_event callback
    scan_state: dict = {"scanned": 0, "total": 1}

    def _show_table() -> None:
        if results:
            results_box.dataframe(
                pd.DataFrame(results),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "URL": st.column_config.LinkColumn("URL", display_text="Open ↗"),
                },
            )

    def on_event(event: dict) -> None:
        etype = event.get("type")

        if etype == "discovering":
            status_box.info(f"Fetching sitemap for {event['site']}…")

        elif etype == "started":
            scan_state["total"] = max(event.get("total", 1), 1)
            status_box.info("Scanning pages (BFS -- total may grow as links are followed)…")

        elif etype == "scanning":
            scan_state["scanned"] = event.get("current", scan_state["scanned"])
            scan_state["total"]   = max(
                event.get("total", scan_state["total"]),
                scan_state["scanned"],
            )
            pct = min(scan_state["scanned"] / scan_state["total"], 0.99)
            progress_bar.progress(
                pct,
                text=(
                    f"{scan_state['scanned']} / ~{scan_state['total']} pages  ·  "
                    f"{len(results)} flagged"
                ),
            )
            url_caption.caption(event.get("url", ""))

        elif etype == "flagged":
            r = event["result"]
            results.append({
                "URL":           r["url"],
                "Page Title":    r["title"],
                "Site":          r["site"],
                "Matched Terms": "; ".join(r["matched_terms"]),
                "Found Via":     r["match_types"],
                "Context":       (r["snippets"] or [""])[0],
            })
            _show_table()
            status_box.info(
                f"Scanning **{scan_state['scanned']} / ~{scan_state['total']}** pages  ·  "
                f"**{len(results)}** flagged so far…"
            )

    try:
        _audit.run_audit_bfs(sites, terms, use_ocr, on_event=on_event)
    except Exception as exc:
        st.error(f"Audit error: {exc}")
        st.stop()

    # ── Finished ──
    url_caption.empty()
    progress_bar.progress(1.0, text="Done")
    status_box.success(f"Audit complete — **{len(results)}** page(s) flagged.")

    if results:
        df   = pd.DataFrame(results)
        slug = terms[0].replace(" ", "_")[:30]
        date = datetime.now().strftime("%Y%m%d")

        _show_table()

        st.download_button(
            label="⬇  Export CSV",
            data=df.to_csv(index=False).encode("utf-8"),
            file_name=f"fn_audit_{slug}_{date}.csv",
            mime="text/csv",
            type="primary",
        )
    else:
        results_box.info("No pages matched the search terms.")
