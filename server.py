#!/usr/bin/env python3
"""
Field Nation Content Auditor — Web Server
==========================================
Runs a local Flask web app so the team can run content audits from a browser.

USAGE:
    python3 server.py
    # Then open http://localhost:5000

DEPENDENCIES (in addition to audit.py requirements):
    pip3 install flask
"""

import json
import logging
import sys
import threading
import time
import uuid
from queue import Empty, Queue
from threading import Event
from typing import Dict

from flask import Flask, Response, jsonify, render_template, request

# Import utilities from the audit module in the same directory
sys.path.insert(0, __file__.rsplit("/", 1)[0])
import audit as _audit

app    = Flask(__name__)
log    = logging.getLogger(__name__)
jobs: Dict[str, dict] = {}   # job_id → job state


# ── Job Runner (background thread) ───────────────────────────────────────────

def _run_job(job_id: str, terms: list, sites: list, use_ocr: bool,
             extra_seeds: list) -> None:
    """
    Runs the full audit (BFS) in a background thread, emitting SSE events via the
    job's Queue.  run_audit_bfs handles Playwright internally (sync_playwright is
    created inside the thread, which is required).
    """
    job = jobs[job_id]
    q: Queue      = job["events"]
    cancel: Event = job["cancel"]

    def emit(event: dict) -> None:
        """Forward audit events to the SSE queue and mirror state into job dict."""
        q.put(event)
        etype = event.get("type")
        if etype == "flagged":
            job["results"].append(event["result"])
        elif etype in ("started", "scanning"):
            # Keep the running total estimate up-to-date for /status endpoint
            job["total"] = event.get("total", job["total"])

    try:
        results = _audit.run_audit_bfs(
            sites, terms, use_ocr,
            on_event=emit,
            cancel_event=cancel,
            extra_seeds=extra_seeds or [],
        )
        status = "cancelled" if cancel.is_set() else "complete"
        job["status"] = status
        q.put({"type": status, "count": len(results)})

    except Exception as exc:
        log.exception("Audit job failed")
        job["status"] = "error"
        q.put({"type": "error", "message": str(exc)})


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", sites=_audit.SITES, ocr_available=_audit.OCR_AVAILABLE)


@app.route("/audit/start", methods=["POST"])
def start_audit():
    data        = request.get_json(force=True)
    terms       = [t.strip() for t in data.get("terms", []) if t.strip()]
    sites       = data.get("sites", ["support"])
    use_ocr     = bool(data.get("use_ocr", False))
    extra_seeds = [u.strip() for u in data.get("extra_seeds", []) if u.strip()]

    if not terms:
        return jsonify({"error": "At least one search term is required."}), 400
    if not sites:
        return jsonify({"error": "At least one site must be selected."}), 400

    # Validate site keys
    for s in sites:
        if s not in _audit.SITES:
            return jsonify({"error": f"Unknown site: {s}"}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status":  "running",
        "results": [],
        "total":   0,
        "events":  Queue(),
        "cancel":  Event(),
    }

    t = threading.Thread(target=_run_job,
                         args=(job_id, terms, sites, use_ocr, extra_seeds),
                         daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/audit/stream/<job_id>")
def stream_audit(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return Response("data: {\"type\":\"error\",\"message\":\"Job not found\"}\n\n",
                        content_type="text/event-stream")

    def generate():
        q: Queue = job["events"]
        last_ping = time.time()

        while True:
            try:
                event = q.get(timeout=1.0)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("complete", "cancelled", "error"):
                    break
            except Empty:
                # Send keepalive ping every 15s so proxies don't close the connection
                if time.time() - last_ping > 15:
                    yield "data: {\"type\":\"ping\"}\n\n"
                    last_ping = time.time()
                # If job finished between polls, exit
                if job["status"] in ("complete", "cancelled", "error") and q.empty():
                    break

    return Response(
        generate(),
        content_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",   # Disable nginx buffering
        },
    )


@app.route("/audit/cancel/<job_id>", methods=["POST"])
def cancel_audit(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    job["cancel"].set()
    return jsonify({"ok": True})


@app.route("/audit/status/<job_id>")
def audit_status(job_id: str):
    """Returns current job state — useful for client reconnect recovery."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status":  job["status"],
        "total":   job["total"],
        "count":   len(job["results"]),
        "results": job["results"],
    })


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  Field Nation Content Auditor")
    print(f"  Open: http://localhost:{port}\n")
    # threaded=True so SSE streams don't block other requests
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
