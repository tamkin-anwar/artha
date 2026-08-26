"""
artha/services/ai_service.py
-----------------------------
Claude API integration for Artha.

Architecture decisions:
  - Lazy singleton client: avoids import-time failure when ANTHROPIC_API_KEY
    is absent in local dev. First call initializes; all subsequent calls reuse.
  - Class-based with classmethods: no instantiation boilerplate at call sites.
  - HTTP-agnostic: all public methods return plain dicts, never Flask responses.
    Routes own HTTP concerns; this service owns AI concerns.
  - Client-owned history: conversation history is sent by the client on every
    request and sanitized here. Server stays stateless — no session bloat.
  - Financial context injected into system prompt on every request. Simple and
    correct for a personal app at this data scale; no RAG needed yet.
  - Streaming via generator: routes own SSE framing; service yields text chunks.

Model choice:
  claude-haiku-4-5-20251001 — fast and cost-effective for a 2-person personal
  app. Override with ARTHA_AI_MODEL env var for experimentation.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Generator

from anthropic import (
    Anthropic,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
)

from ..blueprints.finance.routes import TRANSACTION_CATEGORIES
from ..models import Transaction

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 1024
_MAX_CONTEXT_TRANSACTIONS = 50  # caps context window size and cost
_MAX_HISTORY_TURNS = 20         # max conversation turns accepted from client
_MAX_MESSAGE_LEN = 4000         # character limit per user message

_SYSTEM_PROMPT_TEMPLATE = """\
You are Artha AI, an intelligent personal assistant built into Artha, \
a personal finance and productivity OS.

You are talking to {first_name}. Today is {today}.

## Financial Snapshot
{financial_context}

## Behaviour
- Be concise, warm, and direct. Cut filler and generic advice.
- Reference real numbers from the snapshot whenever relevant.
- Format all currency as $X,XXX.XX.
- If data is missing or a question is outside your knowledge, say so honestly.
- You can help with budgeting, spending analysis, financial planning, \
goal setting, and general productivity.
- Never use em dashes (—). Use a period, comma, or colon instead.
- Use the add_transaction tool only when {first_name} clearly wants a real \
transaction logged (e.g. "log a $12 coffee expense"), never for hypothetical \
questions or questions about past spending. The tool shows a confirmation \
card with the full details, so keep your own reply to one short sentence \
instead of repeating them.
"""

# ---------------------------------------------------------------------------
# Tools — actions the model can propose. Nothing here executes on its own:
# a tool_use block only becomes a "pending_actions" entry in chat()'s return
# value, and the frontend renders it as a confirmation card the user must
# explicitly approve before the existing /add_transaction route ever runs.
# ---------------------------------------------------------------------------

_TOOLS = [
    {
        "name": "add_transaction",
        "description": (
            "Propose adding an income or expense transaction. This never "
            "executes on its own: the user sees a confirmation card and "
            "must explicitly approve it before anything is saved. Only "
            "call this when the user clearly wants a real transaction "
            "logged, not for hypotheticals or questions about past spending."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Short label, e.g. 'Coffee'."},
                "amount": {"type": "number", "description": "Positive amount."},
                "type": {"type": "string", "enum": ["income", "expense"]},
                "category": {"type": "string", "enum": list(TRANSACTION_CATEGORIES.keys())},
                "date": {"type": "string", "description": "YYYY-MM-DD. Omit for today."},
            },
            "required": ["description", "amount", "type"],
        },
    },
]


# ---------------------------------------------------------------------------
# Anthropic client — lazy singleton
# ---------------------------------------------------------------------------

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    """Return the shared Anthropic client, initializing on first call."""
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. "
                "Add it in Render → Environment Variables."
            )
        _client = Anthropic(api_key=api_key)
        log.info("Anthropic client initialized (model=%s).", _get_model())
    return _client


def _get_model() -> str:
    return os.environ.get("ARTHA_AI_MODEL", _DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def _assemble_financial_context(user) -> str:
    """
    Query the DB and return a structured text block describing the user's
    financial position. Injected into the system prompt on every request.
    """
    transactions: list[Transaction] = (
        Transaction.query
        .filter_by(user_id=user.id)
        .order_by(Transaction.timestamp.desc())
        .limit(_MAX_CONTEXT_TRANSACTIONS)
        .all()
    )

    if not transactions:
        return "No transactions recorded yet."

    zero = Decimal("0")
    income_txs  = [t for t in transactions if t.type == "income"]
    expense_txs = [t for t in transactions if t.type == "expense"]

    total_income:  Decimal = sum((t.amount for t in income_txs),  zero)
    total_expense: Decimal = sum((t.amount for t in expense_txs), zero)
    net:           Decimal = total_income - total_expense

    lines = [
        f"Total income:        ${total_income:,.2f}",
        f"Total expenses:      ${total_expense:,.2f}",
        f"Net balance:         ${net:,.2f} ({'surplus' if net >= 0 else 'deficit'})",
        f"Transactions loaded: {len(transactions)} "
        f"(capped at {_MAX_CONTEXT_TRANSACTIONS} most recent)",
        "",
        "Recent transactions (newest first):",
    ]
    for tx in transactions:
        ts   = tx.timestamp.strftime("%b %d, %Y") if tx.timestamp else "—"
        sign = "+" if tx.type == "income" else "−"
        lines.append(f"  {ts}  {sign}${tx.amount:,.2f}  {tx.description}")

    return "\n".join(lines)


def _build_system_prompt(user) -> str:
    first_name        = user.first_name or user.username
    today             = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    financial_context = _assemble_financial_context(user)
    return _SYSTEM_PROMPT_TEMPLATE.format(
        first_name=first_name,
        today=today,
        financial_context=financial_context,
    )


def _sanitize_history(history: list | None) -> list[dict]:
    """
    Validate and trim conversation history received from the client.

    Rejects any entry that isn't a valid {role, content} pair.
    Caps at _MAX_HISTORY_TURNS (20 turns = 40 messages) to control
    prompt size and cost.
    """
    if not isinstance(history, list):
        return []
    valid = [
        {"role": h["role"], "content": str(h["content"])}
        for h in history
        if isinstance(h, dict)
        and h.get("role") in ("user", "assistant")
        and h.get("content")
    ]
    return valid[-(  _MAX_HISTORY_TURNS * 2):]


# ---------------------------------------------------------------------------
# AIService
# ---------------------------------------------------------------------------

class AIService:
    """
    All public methods:
      - Require an active Flask application context (for DB access).
      - Accept a Flask-Login User ORM object as first argument.
      - Return a plain dict — never an HTTP Response or exception.
      - Return {"error": "<human-readable message>"} on any failure.
    """

    # ------------------------------------------------------------------
    # Non-streaming chat  (primary endpoint for Render Starter tier)
    # ------------------------------------------------------------------

    @classmethod
    def chat(
        cls,
        user,
        message: str,
        history: list | None = None,
    ) -> dict:
        """
        Send one chat turn and return the assistant's full reply.

        Args:
            user:    Authenticated User ORM object.
            message: The user's latest message text.
            history: Optional prior conversation as
                     [{"role": "user"|"assistant", "content": "..."}, ...]

        Returns:
            {"reply": str, "pending_actions": [{"type": str, "params": dict}],
             "usage": {"input_tokens": int, "output_tokens": int}}
            {"error": str}

            pending_actions is always present, empty on ordinary turns. Each
            entry is a proposal only: nothing is written to the database
            here. The frontend renders a confirmation card, and only a user
            click submits the params to the real, already-validated write
            route (e.g. /add_transaction).
        """
        if not message or not message.strip():
            return {"error": "Message cannot be empty."}
        if len(message) > _MAX_MESSAGE_LEN:
            return {"error": f"Message exceeds {_MAX_MESSAGE_LEN} character limit."}

        try:
            client = _get_client()
        except RuntimeError as exc:
            log.error("AI client init failed: %s", exc)
            return {"error": str(exc)}

        messages = _sanitize_history(history)
        messages.append({"role": "user", "content": message.strip()})

        try:
            resp = client.messages.create(
                model=_get_model(),
                max_tokens=_MAX_TOKENS,
                system=_build_system_prompt(user),
                messages=messages,
                tools=_TOOLS,
            )

            reply_text = "".join(
                block.text for block in resp.content if block.type == "text"
            )
            pending_actions = [
                {"type": block.name, "params": block.input}
                for block in resp.content
                if block.type == "tool_use"
            ]

            return {
                "reply": reply_text,
                "pending_actions": pending_actions,
                "usage": {
                    "input_tokens":  resp.usage.input_tokens,
                    "output_tokens": resp.usage.output_tokens,
                },
            }

        except APITimeoutError:
            log.warning("Anthropic timeout for user %d.", user.id)
            return {"error": "Request timed out. Please try again."}
        except APIConnectionError as exc:
            log.error("Anthropic connection error: %s", exc)
            return {"error": "Could not reach AI service. Check connectivity."}
        except APIStatusError as exc:
            log.error("Anthropic status error %s: %s", exc.status_code, exc.message)
            return {"error": f"AI service error ({exc.status_code}). Please try again."}
        except Exception as exc:
            log.exception("Unexpected AIService.chat error: %s", exc)
            return {"error": "An unexpected error occurred."}

    # ------------------------------------------------------------------
    # Streaming chat
    # ------------------------------------------------------------------

    @classmethod
    def stream_chat(
        cls,
        user,
        message: str,
        history: list | None = None,
    ) -> Generator[str, None, None]:
        """
        Stream the assistant reply as text delta chunks.

        Yields plain text strings. On error, yields a single string
        prefixed with "ERROR:" so the route can detect it cleanly.

        Note on Render Starter (sync Gunicorn workers):
            True character-by-character streaming requires async workers
            (eventlet/gevent). With sync workers the full response is buffered
            before sending. The /chat endpoint is the safe primary choice;
            /chat/stream is available for when async workers are configured.

        Route usage pattern:
            def generate():
                for chunk in AIService.stream_chat(user, msg, hist):
                    if chunk.startswith("ERROR:"):
                        yield f"event: error\\ndata: {chunk[6:]}\\n\\n"
                        return
                    yield f"data: {json.dumps(chunk)}\\n\\n"
                yield "data: [DONE]\\n\\n"

            return Response(stream_with_context(generate()),
                            mimetype="text/event-stream",
                            headers={"Cache-Control": "no-cache",
                                     "X-Accel-Buffering": "no"})
        """
        if not message or not message.strip():
            yield "ERROR:Message cannot be empty."
            return
        if len(message) > _MAX_MESSAGE_LEN:
            yield f"ERROR:Message exceeds {_MAX_MESSAGE_LEN} character limit."
            return

        try:
            client = _get_client()
        except RuntimeError as exc:
            yield f"ERROR:{exc}"
            return

        messages = _sanitize_history(history)
        messages.append({"role": "user", "content": message.strip()})

        try:
            with client.messages.stream(
                model=_get_model(),
                max_tokens=_MAX_TOKENS,
                system=_build_system_prompt(user),
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    yield text

        except APITimeoutError:
            yield "ERROR:Request timed out. Please try again."
        except APIConnectionError as exc:
            log.error("Stream connection error: %s", exc)
            yield "ERROR:Could not reach AI service."
        except APIStatusError as exc:
            log.error("Stream API error %s: %s", exc.status_code, exc.message)
            yield f"ERROR:AI service error ({exc.status_code})."
        except Exception as exc:
            log.exception("Unexpected stream error: %s", exc)
            yield "ERROR:An unexpected error occurred."

    # ------------------------------------------------------------------
    # Financial insights  (no user prompt required)
    # ------------------------------------------------------------------

    @classmethod
    def get_financial_insights(cls, user) -> dict:
        """
        Auto-generate a structured financial health report from the user's
        transaction data. No user prompt needed — fires a fixed analytical
        prompt against the assembled financial snapshot.

        Returns:
            {"insights": str, "summary": dict, "usage": dict}
            {"error": str}
        """
        prompt = (
            "Give me a focused financial health check based on my data above:\n\n"
            "1. **Health Assessment** (2–3 sentences): Overall picture.\n"
            "2. **Key Patterns** (2–3 bullets): Notable spending or income trends.\n"
            "3. **Top Action** (1 sentence): One specific, actionable next step.\n\n"
            "Be specific with numbers from my snapshot. No generic advice."
        )

        result = cls.chat(user, prompt, history=None)
        if "error" in result:
            return result

        # Build a structured summary for the UI to consume alongside the prose.
        transactions = Transaction.query.filter_by(user_id=user.id).all()
        zero         = Decimal("0")
        total_income  = sum((t.amount for t in transactions if t.type == "income"),  zero)
        total_expense = sum((t.amount for t in transactions if t.type == "expense"), zero)

        return {
            "insights": result["reply"],
            "summary": {
                "total_income":      float(total_income),
                "total_expenses":    float(total_expense),
                "net":               float(total_income - total_expense),
                "transaction_count": len(transactions),
            },
            "usage": result.get("usage"),
        }
