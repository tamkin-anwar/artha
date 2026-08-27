"""
artha/blueprints/ai/routes.py
------------------------------
HTTP layer for all AI features.

Endpoints:
  POST /api/ai/conversation/new  — start a fresh conversation (Clear button)
  POST /api/ai/chat              — single or multi-turn chat (JSON in / JSON out)
  POST /api/ai/insights          — auto-generate financial health report
  POST /api/ai/chat/stream       — SSE streaming chat

CSRF:
  All POST endpoints are protected by Flask-WTF via the X-CSRFToken header.
  Frontend must include: headers: { "X-CSRFToken": window.CSRF_TOKEN }
  (CSRF_TOKEN is already injected into all templates via inject_csrf_token.)

Conversation history contract:
  The server persists history (Conversation/Message models), but the
  frontend never rehydrates it — every fresh page load of /ai starts
  showing the empty state, including just navigating to another page
  (Notes, Finance, ...) and back. That's a deliberate choice, not a
  missing feature: this isn't a general-purpose chatbot people file
  conversations away in, and a page that always starts fresh is simpler
  than one that sometimes drags in a stale exchange from an earlier visit.

  What the DB persistence is actually for: _get_or_create_conversation()
  keeps appending to the same conversation as long as it's been active
  within _CONVERSATION_IDLE_MINUTES, so the model still has real
  short-term memory for a quick back-and-forth (including a brief detour
  to another tab and back) — it just isn't shown on screen. Go quiet for
  longer than that and the next message starts a genuinely fresh
  conversation, with no old context carried in either visibly or to the
  model. /chat's request body is just {"message": "..."}; the route loads
  the current conversation's recent messages from the DB before calling
  AIService, and persists both sides of the turn after. /chat/stream
  still accepts a client-supplied "history" the old way; it's not wired
  into any UI (see its own docstring), so it wasn't worth carrying the
  same rework for code nothing calls.

Error shape:
  All errors return JSON: { "error": "<human-readable message>" }
  4xx — bad client input.
  503 — AI service unavailable or returned an error.
"""

import json
import logging
from datetime import datetime, timedelta

from flask import Response, jsonify, render_template, request, stream_with_context
from flask_login import current_user, login_required

from ...extensions import db
from ...models import Conversation, Message
from ...services.ai_service import AIService
from . import ai_bp

log = logging.getLogger(__name__)

# How long a conversation can go quiet before the next message starts a
# fresh one instead of continuing it. Long enough that a quick detour to
# another tab and back doesn't lose the thread; short enough that coming
# back later doesn't drag in a stale, invisible exchange the model would
# reference but the page never shows.
_CONVERSATION_IDLE_MINUTES = 30


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


def _parse_message() -> str | None:
    """Extract and lightly validate just the message from a JSON body —
    /chat no longer accepts client-supplied history (see module docstring)."""
    data = request.get_json(silent=True) or {}
    return (data.get("message") or "").strip() or None


def _current_conversation() -> Conversation | None:
    """The user's most recently created conversation, or None if they've
    never chatted (or just hit Clear and haven't sent anything since).
    Ordered by id, not created_at — two Conversation rows created back to
    back (Clear immediately followed by the next page load's GET) could
    otherwise land on the same timestamp at typical datetime resolution;
    the auto-incrementing primary key can't tie."""
    return (
        Conversation.query
        .filter_by(user_id=current_user.id)
        .order_by(Conversation.id.desc())
        .first()
    )


def _get_or_create_conversation() -> Conversation:
    """The conversation a new message/insights report should be appended
    to. Starts a fresh one if there's no current conversation yet, or if
    the current one's last message is older than _CONVERSATION_IDLE_MINUTES
    — see module docstring for why that's the actual memory boundary now,
    not "did the user navigate away and come back"."""
    conversation = _current_conversation()

    if conversation is not None:
        last_message = (
            Message.query
            .filter_by(conversation_id=conversation.id)
            .order_by(Message.id.desc())
            .first()
        )
        if last_message is not None:
            # datetime.utcnow() (naive), not datetime.now(timezone.utc):
            # DateTime columns round-trip through SQLite as naive values
            # regardless of the aware default used at insert time, so
            # comparing against an aware "now" would raise on subtraction.
            # Matches the same naive-UTC convention cli.py's trash purge
            # already uses for this exact kind of "how long ago" check.
            idle_for = datetime.utcnow() - last_message.created_at
            if idle_for > timedelta(minutes=_CONVERSATION_IDLE_MINUTES):
                conversation = None

    if conversation is None:
        conversation = Conversation(user_id=current_user.id)
        db.session.add(conversation)
        db.session.flush()  # assigns conversation.id for the Messages below

    return conversation


def _load_history(conversation: Conversation) -> list[dict]:
    """The conversation's messages, oldest first, capped the same way
    AIService's own _sanitize_history() caps client-supplied history —
    just applied at load time now that the DB is the source of truth.
    Ordered by id, not created_at — see models/conversation.py.

    Queries Message directly rather than through conversation.messages:
    that relationship already carries its own baked-in ascending
    order_by, and Query.order_by() on a dynamic relationship *appends* to
    an existing order rather than replacing it — chaining .desc() there
    silently no-ops instead of reversing anything.
    """
    rows = (
        Message.query
        .filter_by(conversation_id=conversation.id)
        .order_by(Message.id.desc())
        .limit(AIService.MAX_HISTORY_MESSAGES)
        .all()
    )
    rows.reverse()
    return [{"role": m.role, "content": m.content} for m in rows]


def _save_turn(conversation: Conversation, user_message: str, reply: str) -> None:
    # Flushed separately, not both added then committed together: a single
    # flush of two same-table inserts doesn't reliably assign ids in the
    # order they were added (see models/conversation.py's own note on why
    # ordering by id rather than created_at matters) — flushing the user
    # message first guarantees it actually gets the lower id.
    db.session.add(Message(conversation_id=conversation.id, role="user", content=user_message))
    db.session.flush()
    if reply and reply.strip():
        db.session.add(Message(conversation_id=conversation.id, role="assistant", content=reply))
    db.session.commit()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@ai_bp.post("/conversation/new")
@login_required
def new_conversation():
    """
    Starts a fresh conversation (the Clear button). Older conversations
    are kept, just no longer "current" — see models/conversation.py.
    """
    conversation = Conversation(user_id=current_user.id)
    db.session.add(conversation)
    db.session.commit()
    return jsonify({"message": "Started a new conversation"}), 201


@ai_bp.post("/chat")
@login_required
def chat():
    """
    Non-streaming chat.

    Request  (JSON): { "message": "..." }
    Response (JSON): { "reply": "...", "pending_actions": [...],
                        "usage": { "input_tokens": N, "output_tokens": N } }
                  or { "error": "..." }

    pending_actions is empty on ordinary turns. Each entry
    ({"type": "add_transaction", "params": {...}}) is a proposal the AI
    is not allowed to execute itself: the frontend renders a confirmation
    card, and only a user click submits it to the real write route.

    History is loaded from and persisted to the current conversation
    server-side (see module docstring) — the frontend no longer sends or
    tracks it.
    """
    message = _parse_message()
    if not message:
        return _bad_request("message is required and cannot be empty.")

    conversation = _get_or_create_conversation()
    history = _load_history(conversation)

    result = AIService.chat(current_user, message, history)

    if "error" in result:
        log.error("AIService.chat error (user=%d): %s", current_user.id, result["error"])
        return _service_error(result["error"])

    _save_turn(conversation, message, result.get("reply", ""))

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

    Saved to the current conversation as an assistant-only message (no
    preceding user turn), same as a normal reply — feeds the same
    short-term, not-shown-on-screen memory described in the module
    docstring if you ask a follow-up shortly after.
    """
    result = AIService.get_financial_insights(current_user)

    if "error" in result:
        log.error("AIService.insights error (user=%d): %s", current_user.id, result["error"])
        return _service_error(result["error"])

    conversation = _get_or_create_conversation()
    db.session.add(Message(conversation_id=conversation.id, role="assistant", content=result["insights"]))
    db.session.commit()

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
