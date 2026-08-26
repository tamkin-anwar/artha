"""
artha/blueprints/ai/routes.py
------------------------------
HTTP layer for all AI features.

Endpoints:
  POST /api/ai/chat           — single or multi-turn chat (JSON in / JSON out)
  POST /api/ai/insights       — auto-generate financial health report
  POST /api/ai/chat/stream    — SSE streaming chat

CSRF:
  All POST endpoints are protected by Flask-WTF via the X-CSRFToken header.
  Frontend must include: headers: { "X-CSRFToken": window.CSRF_TOKEN }
  (CSRF_TOKEN is already injected into all templates via inject_csrf_token.)

Conversation history contract:
  The client owns and maintains history. On every request, send the full
  prior conversation as "history": [{"role": ..., "content": ...}, ...].
  The service sanitizes and caps it; the server never stores it.

Error shape:
  All errors return JSON: { "error": "<human-readable message>" }
  4xx — bad client input.
  503 — AI service unavailable or returned an error.
"""

import json
import logging

from flask import Response, jsonify, render_template, request, stream_with_context
from flask_login import current_user, login_required

from ...services.ai_service import AIService
from . import ai_bp

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _bad_request(msg: str):
    return jsonify({"error": msg}), 400


def _service_error(msg: str):
    return jsonify({"error": msg}), 503


def _parse_body() -> tuple[str | None, list]:
    """Extract and lightly validate message + history from JSON body."""
    data    = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip() or None
    history = data.get("history") if isinstance(data.get("history"), list) else []
    return message, history


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@ai_bp.post("/chat")
@login_required
def chat():
    """
    Non-streaming chat.

    Request  (JSON): { "message": "...", "history": [...] }
    Response (JSON): { "reply": "...", "pending_actions": [...],
                        "usage": { "input_tokens": N, "output_tokens": N } }
                  or { "error": "..." }

    pending_actions is empty on ordinary turns. Each entry
    ({"type": "add_transaction", "params": {...}}) is a proposal the AI
    is not allowed to execute itself: the frontend renders a confirmation
    card, and only a user click submits it to the real write route.

    The client appends the reply to its local history and sends the updated
    history on the next request. The server is fully stateless.
    """
    message, history = _parse_body()
    if not message:
        return _bad_request("message is required and cannot be empty.")

    result = AIService.chat(current_user, message, history)

    if "error" in result:
        log.error("AIService.chat error (user=%d): %s", current_user.id, result["error"])
        return _service_error(result["error"])

    return jsonify(result), 200


@ai_bp.post("/insights")
@login_required
def insights():
    """
    Auto-generate a financial health report.

    No request body required. The service assembles context from the
    user's transactions and fires a fixed analytical prompt.

    Response (JSON):
        {
          "insights": "<markdown prose>",
          "summary": {
            "total_income": float,
            "total_expenses": float,
            "net": float,
            "transaction_count": int
          },
          "usage": { "input_tokens": N, "output_tokens": N }
        }
    """
    result = AIService.get_financial_insights(current_user)

    if "error" in result:
        log.error("AIService.insights error (user=%d): %s", current_user.id, result["error"])
        return _service_error(result["error"])

    return jsonify(result), 200


@ai_bp.post("/chat/stream")
@login_required
def stream_chat():
    """
    SSE streaming chat.

    Request  (JSON): { "message": "...", "history": [...] }

    Stream events:
      data: <json-encoded text chunk>   — content delta (JSON.parse on client)
      event: error / data: <message>    — something went wrong mid-stream
      data: [DONE]                      — stream complete

    Client-side EventSource usage:
        const es = new EventSource(/* POST not supported by EventSource */);

    Because EventSource only supports GET, use fetch() with a ReadableStream
    or an SSE-compatible fetch library. Example:

        const res = await fetch("/api/ai/chat/stream", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": window.CSRF_TOKEN,
            },
            body: JSON.stringify({ message, history }),
        });
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split("\\n\\n");
            buffer = lines.pop();
            for (const line of lines) {
                if (line.startsWith("data: ")) {
                    const payload = line.slice(6);
                    if (payload === "[DONE]") { /* finalise */ break; }
                    outputEl.textContent += JSON.parse(payload);
                }
                if (line.startsWith("event: error")) { /* handle */ }
            }
        }

    Streaming note (Render Starter):
        True character-by-character streaming requires async Gunicorn workers
        (eventlet/gevent). Sync workers buffer the full response before sending.
        Use /api/ai/chat for reliable UX on the current Starter tier config.
        Add X-Accel-Buffering: no to bypass Nginx buffering when async workers
        are eventually configured.
    """
    message, history = _parse_body()
    if not message:
        return _bad_request("message is required and cannot be empty.")

    # Detach from the LocalProxy before entering the generator so the user
    # object is available even if the request context shifts mid-stream.
    user = current_user._get_current_object()

    def generate():
        for chunk in AIService.stream_chat(user, message, history):
            if chunk.startswith("ERROR:"):
                error_msg = chunk[len("ERROR:"):]
                log.error("Stream error (user=%d): %s", user.id, error_msg)
                yield f"event: error\ndata: {json.dumps(error_msg)}\n\n"
                return
            # JSON-encode each chunk so newlines inside deltas don't break
            # the SSE "data: ...\n\n" framing.
            yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",   # tell Nginx/Render not to buffer
            "Connection":       "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Full-page route — lives outside the /api/ai prefix
# ---------------------------------------------------------------------------
#
# This blueprint is mounted with url_prefix="/api/ai" (see ./__init__.py),
# which is right for the JSON endpoints above but wrong for a page a user
# navigates to directly. `record_once` lets us register a plain "/ai" rule
# straight on the app object at blueprint-registration time, bypassing the
# blueprint's own prefix, without touching any other file.

@login_required
def page():
    """Full-page AI Assistant experience at /ai."""
    return render_template("ai.html")


@ai_bp.record_once
def _register_page_route(state):
    state.app.add_url_rule("/ai", endpoint="ai.page", view_func=page, methods=["GET"])
