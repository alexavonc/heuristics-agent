import os
import queue
import threading
import uuid
import json
import httpx
import anthropic
from flask import Flask, request, Response, jsonify, send_from_directory
from flask_cors import CORS

from analyze import (
    analyze_url,
    analyze_journey,
    analyze_screenshots,
    analyze_journey_screenshots,
    GENERAL_CHAT_SYSTEM_PROMPT,
    _safe_bytes,
)
from benchmark import run_benchmark, capture_with_login, _to_png_b64

app = Flask(__name__)
CORS(app)

API_KEY      = os.environ.get("ANTHROPIC_API_KEY", "")
API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:5000")

# In-memory report store  {report_id: {html, report_text, ...}}
_reports: dict = {}


# ── Frontend ──────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ── Health ────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ── Analyze: live URL ─────────────────────────────────────────────
@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    data  = request.get_json(force=True)
    url   = data.get("url")
    steps = data.get("steps")

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
            mobile_file = request.files.get("mobile")
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
        "score":         result.get("score"),
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


# ── Benchmark (real SSE streaming via thread + queue) ─────────────
@app.route("/api/benchmark", methods=["POST"])
def api_benchmark():
    """
    Body (JSON): { "report_id": "..." }

    Returns text/event-stream:
      data: {"type": "progress", "message": "..."}   — live status (many)
      data: {"type": "complete", "report_id": "..."}  — success
      data: {"type": "error",    "message": "..."}    — failure
    """
    data      = request.get_json(force=True)
    report_id = data.get("report_id", "")

    report = _reports.get(report_id)
    if not report:
        return jsonify({"error": "Report not found"}), 404

    report_text = report.get("report_text", "")
    if not report_text:
        return jsonify({"error": "No report text available for benchmarking"}), 400

    # Use a queue so run_benchmark (running in a thread) can push progress
    # events to the SSE generator in real time.
    msg_queue: queue.Queue = queue.Queue()

    def _cb(msg: str):
        msg_queue.put(("progress", msg))

    def _worker():
        try:
            result = run_benchmark(
                report_text=report_text,
                api_url=API_BASE_URL,
                progress_cb=_cb,
            )
            msg_queue.put(("complete", result))
        except Exception as exc:
            msg_queue.put(("error", str(exc)))

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

    def _gen():
        while True:
            try:
                event_type, payload = msg_queue.get(timeout=120)
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Benchmark timed out'})}\n\n"
                break

            if event_type == "progress":
                yield f"data: {json.dumps({'type': 'progress', 'message': payload})}\n\n"

            elif event_type == "complete":
                bench_id = str(uuid.uuid4())
                _reports[bench_id] = {
                    "html":            payload["html"],
                    "report_text":     "",
                    "product_context": payload.get("product_context", {}),
                    "competitors":     payload.get("competitors", []),
                }
                yield f"data: {json.dumps({'type': 'complete', 'report_id': bench_id})}\n\n"
                break

            elif event_type == "error":
                yield f"data: {json.dumps({'type': 'error', 'message': payload})}\n\n"
                break

    return Response(
        _gen(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Benchmark: login-based re-capture ────────────────────────────
@app.route("/api/benchmark/login-capture", methods=["POST"])
def api_login_capture():
    """
    Body: { comp_url, username, password, workflow_tasks, workflow_name }
    Returns: { steps: [{label, url, screenshot_b64}] }
    """
    data           = request.get_json(force=True)
    comp_url       = data.get("comp_url", "")
    username       = data.get("username", "")
    password       = data.get("password", "")
    workflow_tasks = data.get("workflow_tasks", [])
    workflow_name  = data.get("workflow_name", "workflow")

    if not comp_url or not username:
        return jsonify({"error": "comp_url and username are required"}), 400

    try:
        steps = capture_with_login(comp_url, username, password, workflow_tasks, workflow_name)
        result = []
        for s in steps:
            sb = s.get("screenshot_bytes")
            result.append({
                "label":          s.get("label", ""),
                "url":            s.get("url", ""),
                "screenshot_b64": _to_png_b64(sb) if sb else None,
            })
        return jsonify({"steps": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
