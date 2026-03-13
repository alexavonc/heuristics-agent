import os
import uuid
import json
import httpx
import anthropic
from flask import Flask, request, Response, jsonify
from flask_cors import CORS

from analyze import (
    analyze_url,
    analyze_journey,
    analyze_screenshots,
    analyze_journey_screenshots,
    GENERAL_CHAT_SYSTEM_PROMPT,
    _safe_bytes,
)

app = Flask(__name__)
CORS(app)

API_KEY     = os.environ.get("ANTHROPIC_API_KEY", "")
API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:5000")

# In-memory report store  {report_id: {html, report_text, ...}}
_reports: dict = {}


# ── Health ────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ── Analyze: live URL ─────────────────────────────────────────────
@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    """
    Body (JSON):
      { "url": "https://...", "steps": [...] }   # steps optional — omit for single-page
    """
    data  = request.get_json(force=True)
    url   = data.get("url")
    steps = data.get("steps")  # present → journey mode

    if not url:
        return jsonify({"error": "url is required"}), 400

    try:
        if steps:
            result = analyze_journey(url, steps, api_url=API_BASE_URL)
        else:
            result = analyze_url(url, api_url=API_BASE_URL)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    report_id = str(uuid.uuid4())
    _reports[report_id] = result
    return jsonify({
        "report_id":     report_id,
        "desktop_score": result.get("desktop_score"),
        "mobile_score":  result.get("mobile_score"),
    })


# ── Analyze: screenshot upload ────────────────────────────────────
@app.route("/api/analyze/screenshots", methods=["POST"])
def api_analyze_screenshots():
    """
    Multipart form fields:
      journey=true|false
      desktop  — desktop screenshot file (single-page mode)
      mobile   — mobile screenshot file  (optional, single-page mode)
      steps[]  — multiple screenshot files in order (journey mode)
    """
    journey = request.form.get("journey", "false").lower() == "true"

    try:
        if journey:
            files       = request.files.getlist("steps[]")
            step_images = [f.read() for f in files]
            if not step_images:
                return jsonify({"error": "No step images provided"}), 400
            result = analyze_journey_screenshots(step_images, api_url=API_BASE_URL)
        else:
            desktop_file = request.files.get("desktop")
            if not desktop_file:
                return jsonify({"error": "desktop image is required"}), 400
            mobile_file  = request.files.get("mobile")
            result = analyze_screenshots(
                desktop_file.read(),
                mobile_file.read() if mobile_file else None,
                api_url=API_BASE_URL,
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    report_id = str(uuid.uuid4())
    _reports[report_id] = result
    return jsonify({
        "report_id":     report_id,
        "desktop_score": result.get("desktop_score"),
        "mobile_score":  result.get("mobile_score"),
        "score":         result.get("score"),  # journey screenshot mode
    })


# ── Fetch report HTML ─────────────────────────────────────────────
@app.route("/api/report/<report_id>")
def get_report(report_id):
    report = _reports.get(report_id)
    if not report:
        return jsonify({"error": "Report not found"}), 404
    html = report.get("html", "<h1>No HTML available</h1>")
    return Response(_safe_bytes(html), 200, {"Content-Type": "text/html; charset=utf-8"})


# ── Fetch report JSON data ────────────────────────────────────────
@app.route("/api/report/<report_id>/data")
def get_report_data(report_id):
    report = _reports.get(report_id)
    if not report:
        return jsonify({"error": "Report not found"}), 404
    return jsonify({
        "report_id":     report_id,
        "desktop_score": report.get("desktop_score"),
        "mobile_score":  report.get("mobile_score"),
        "score":         report.get("score"),
        "desktop_locs":  report.get("desktop_locs", []),
        "mobile_locs":   report.get("mobile_locs", []),
    })


# ── Chat (SSE stream) ─────────────────────────────────────────────
@app.route("/api/chat", methods=["POST"])
def chat():
    """
    Body (JSON):
      { "report_id": "...", "messages": [{role, content}, ...] }
    Returns: text/event-stream
    """
    data      = request.get_json(force=True)
    report_id = data.get("report_id", "")
    messages  = data.get("messages", [])

    report      = _reports.get(report_id, {})
    report_text = report.get("report_text", "No report available.")
    system      = GENERAL_CHAT_SYSTEM_PROMPT.format(report_text=report_text)

    def _gen():
        client = anthropic.Anthropic(
            api_key=API_KEY,
            http_client=httpx.Client(verify=False),
        )
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=system,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                yield f"data: {json.dumps({'text': text})}\n\n"
        yield "data: [DONE]\n\n"

    return Response(
        _gen(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
