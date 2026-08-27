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
  - Server-owned history: Conversation/Message (artha/models/) hold it now,
    not the client — needed for the same conversation to follow a user
    across devices. This module's own chat()/stream_chat() are unaware of
    that: the route layer (blueprints/ai/routes.py) loads history from the
    DB and passes it in the same {"role", "content"} shape a client used to.
  - Financial context injected into system prompt on every request. Simple and
    correct for a personal app at this data scale; no RAG needed yet.
  - Streaming via generator: routes own SSE framing; service yields text chunks.

Model choice:
  claude-haiku-4-5 — cost over quality, deliberately: at Artha's current
  usage a much pricier model isn't worth it. The context it gets was
  widened regardless (budgets, upcoming notes/events, active scenarios,
  not just a raw transaction list) since a smaller model benefits more,
  not less, from being handed the right data instead of having to infer
  it. Override with ARTHA_AI_MODEL env var (e.g. up to claude-sonnet-5) if
  quality ever needs to trump cost.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Generator

from anthropic import (
    Anthropic,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
)

from ..blueprints.dashboard.routes import EVENT_COLORS, EVENT_RECURRENCES
from ..blueprints.finance.routes import TRANSACTION_CATEGORIES
from ..blueprints.notes.routes import NOTE_COLORS
from ..models import Event, Note, Transaction
from ..models.budget import Budget
from ..models.category_budget import CategoryBudget
from ..models.scenario import Scenario
from ..utils import user_now

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "claude-haiku-4-5"
_MAX_TOKENS = 2048
_MAX_CONTEXT_TRANSACTIONS = 50  # caps context window size and cost
_MAX_HISTORY_TURNS = 20         # max conversation turns accepted from client
_MAX_MESSAGE_LEN = 4000         # character limit per user message

# Statement-PDF extraction (extract_pdf_transactions, called from
# blueprints/finance/routes.py as a fallback when the deterministic
# line-regex parser in _parse_statement_pdf finds nothing — see that
# function's docstring for why no single deterministic approach covers
# real-world statement layouts). A long statement can run to 100+ rows;
# each renders as a small JSON object in the tool call, so the output cap
# needs real headroom, not the 2048 a short chat reply gets by with.
_STATEMENT_MAX_TOKENS = 8192
# Roughly 10-15 pages of statement text — far past any normal single
# monthly statement, there mainly to bound worst-case cost/latency on a
# pathological upload rather than to ever actually bind in practice.
_STATEMENT_MAX_CHARS = 60_000

# categorize_transactions: one short category string per description, so
# nowhere near _STATEMENT_MAX_TOKENS worth of output is ever needed even
# for a large batch.
_CATEGORIZE_MAX_TOKENS = 4096
# The caller (import_preview) also caps how many uncategorized rows it
# ever sends in one call — see _CATEGORIZE_MAX_ITEMS in
# blueprints/finance/routes.py — this is just this module's own
# independent ceiling on output size, kept in the same spot as the
# statement-extraction constants above for one place to look.

_SYSTEM_PROMPT_TEMPLATE = """\
You are Artha AI, an intelligent personal assistant built into Artha, \
a personal finance and productivity OS.

You are talking to {first_name}. Today is {today}.

## Financial Snapshot
{financial_context}

## Budgets
{budget_context}

## Coming Up
{upcoming_context}

## Active Scenarios
{scenario_context}

## Behaviour
- Be concise, warm, and direct. Cut filler and generic advice.
- Reference real numbers from the Financial Snapshot, Budgets, Coming Up, \
and Active Scenarios sections above whenever relevant, not just the raw \
transaction list. If asked whether something is affordable or on track, \
check it against the actual budget and scenario numbers you have instead \
of a generic estimate.
- Format all currency as $X,XXX.XX.
- If data is missing or a question is outside your knowledge, say so honestly.
- You can help with budgeting, spending analysis, financial planning, \
goal setting, and general productivity.
- Never use em dashes (—). Use a period, comma, or colon instead.
- Use add_transaction, create_note, create_event, or set_budget only when \
{first_name} clearly wants that real thing logged, written down, scheduled, \
or capped, never for hypotheticals, brainstorming out loud, or questions \
about existing data. Each tool shows a confirmation card with the full \
details, so keep your own reply to one short sentence instead of repeating \
them.
- For create_event, always give start and end as real YYYY-MM-DDTHH:MM:SS \
values worked out from today's date, never a vague phrase like "tomorrow."
- You have no calculator tool: any math beyond simple arithmetic (a loan \
payment, compound interest, amortization) is you reasoning it out, not a \
verified computation. For that kind of math, show the formula and the \
numbers you plugged in rather than stating just the final result, so a \
wrong number is easy to catch instead of invisible. For anything the \
user wants to be sure is exactly right, point them to the Calculator \
page, which computes it directly instead of reasoning about it.
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
                "is_recurring": {
                    "type": "boolean",
                    "description": (
                        "True only if the user describes this as an ongoing bill or "
                        "income that repeats every month (rent, a subscription, a "
                        "paycheck) — never for a one-off purchase. Recurs monthly on "
                        "this transaction's own day of month; there's no other "
                        "cadence to choose. Omit (defaults to false) if unsure."
                    ),
                },
            },
            "required": ["description", "amount", "type"],
        },
    },
    {
        "name": "create_note",
        "description": (
            "Propose creating a note. This never executes on its own: the "
            "user sees a confirmation card and must explicitly approve it "
            "before anything is saved. Only call this when the user "
            "clearly wants something written down, not for brainstorming "
            "out loud."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string"},
                "color": {"type": "string", "enum": list(NOTE_COLORS)},
                "due_date": {"type": "string", "description": "YYYY-MM-DD. Omit if none."},
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "create_event",
        "description": (
            "Propose creating a calendar event. This never executes on its "
            "own: the user sees a confirmation card and must explicitly "
            "approve it before anything is saved. Only call this when the "
            "user clearly wants something scheduled."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start": {"type": "string", "description": "YYYY-MM-DDTHH:MM:SS, local time, no timezone."},
                "end": {"type": "string", "description": "YYYY-MM-DDTHH:MM:SS, local time, no timezone."},
                "color": {"type": "string", "enum": list(EVENT_COLORS)},
                "recurrence": {"type": "string", "enum": list(EVENT_RECURRENCES)},
            },
            "required": ["title", "start", "end"],
        },
    },
    {
        "name": "set_budget",
        "description": (
            "Propose a monthly spending budget, either overall or for one "
            "category. This never executes on its own: the user sees a "
            "confirmation card and must explicitly approve it before "
            "anything is saved. Only call this when the user clearly wants "
            "a budget cap set, not for questions about existing budgets "
            "or spending."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Positive monthly cap."},
                "category": {
                    "type": "string",
                    "enum": [key for key in TRANSACTION_CATEGORIES if key != "income"],
                    "description": "Omit for the overall monthly budget, not one category.",
                },
            },
            "required": ["amount"],
        },
    },
]

# ---------------------------------------------------------------------------
# Statement-PDF extraction tool — forced via tool_choice, never offered
# alongside _TOOLS. This is a one-shot structured-extraction call (read the
# page text, hand back transactions), not a proposal the user confirms —
# extract_pdf_transactions()'s caller (blueprints/finance/routes.py) still
# runs every returned row through the same preview-and-edit table a
# regex-parsed CSV/PDF import goes through before anything is saved, so
# this doesn't skip the existing human-review safety net.
# ---------------------------------------------------------------------------

_STATEMENT_SYSTEM_PROMPT = """\
You read bank/card statement text extracted from a PDF and return every \
real transaction as structured data. The text was pulled from a table \
layout, so it often arrives jumbled: a transaction's date and description \
may sit on one line while its amount and running balance sit on another, \
several lines down, with no date on that line at all. Read the whole \
document as a human reading a printed statement would, not line by line.

Rules:
- One entry per real movement of money. Never include a pure "Opening \
Balance" / "Balance Brought Forward" / running-balance-only line that has \
no actual debit or credit amount, and never include a totals/summary row \
printed at the end of a statement.
- date: always output ISO YYYY-MM-DD, converting from whatever format the \
statement uses (DD/MM/YYYY, MM/DD/YYYY, "19 Sep 2024", etc.) — infer \
day-first vs month-first from context (a day > 12 anywhere in the \
document settles it; otherwise default to the format most of the \
document's other unambiguous dates use).
- description: the merchant/counterparty/memo text for that row, with \
line-wrap artifacts joined into one clean line. Drop boilerplate that \
isn't part of the description itself (page footers, "Page X of Y", the \
bank's own disclaimer text).
- amount: a positive number — the transaction's own magnitude, never the \
running balance column.
- type: "expense" for a debit/withdrawal, "income" for a credit/deposit. \
Most statements print these as separate Debit/Credit (or \
Withdrawal/Deposit) columns — use whichever column is non-zero. If \
there's a single signed amount column instead, negative is expense, \
positive is income.
- Copy every amount and date exactly as printed, just reformatted per the \
rules above — never estimate, round, or invent a value that isn't \
directly in the source text.
- If you genuinely find no transactions, call the tool with an empty list \
rather than declining to call it.
"""

_STATEMENT_TOOL = {
    "name": "record_transactions",
    "description": "Record every real transaction found in the statement text.",
    "input_schema": {
        "type": "object",
        "properties": {
            "transactions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "YYYY-MM-DD"},
                        "description": {"type": "string"},
                        "amount": {"type": "number", "description": "Positive."},
                        "type": {"type": "string", "enum": ["income", "expense"]},
                    },
                    "required": ["date", "description", "amount", "type"],
                },
            },
        },
        "required": ["transactions"],
    },
}

# ---------------------------------------------------------------------------
# Statement-import categorization — fills in the category for whatever an
# import left uncategorized. The fixed keyword list in
# blueprints/finance/routes.py (_CATEGORY_KEYWORDS) is free, instant, and
# handles the common-merchant case fine, but it's necessarily a finite list
# of (mostly US) brand names — it can't keep pace with every merchant in
# every country, and it has genuinely nothing to work with on a statement
# that never prints a merchant name at all (a masked-card "POS purchase"
# line, common outside the US). An LLM's broader pattern recognition picks
# up real merchant names the fixed list doesn't happen to include, and can
# at least recognize a transaction's *type* (a bank transfer, a fee) even
# with no merchant name — see the "null" instruction below for what it's
# told to do when even that isn't inferable, which is the common case for
# a masked-card line and is treated as a correct outcome, not a shortfall.
# ---------------------------------------------------------------------------

_CATEGORIZE_SYSTEM_PROMPT = """\
You assign a spending category to each bank/card transaction description \
below, from this fixed list only: {categories}

Rules:
- One category per transaction, or null if you genuinely can't tell.
- Recognize real merchants/services by name even in abbreviated, \
non-English, or unfamiliar-brand form — a keyword list can't keep up \
with every merchant worldwide, which is why you're being asked instead.
- A transaction can carry real signal even with no merchant name: a bank \
transfer, remittance, wire, or account-to-account move; a bank fee, \
service charge, or annual card fee; an ATM withdrawal. None of these map \
to a specific spending category (you have no idea what an ATM \
withdrawal was actually spent on), so use "other" for them — that's a \
real classification, distinct from null.
- Use null only when the description gives no signal at all beyond \
"a card was used somewhere" — a masked-card POS/purchase line with no \
merchant name anywhere in it (common on statements that mask the \
merchant for privacy) is the standard case. Guessing a specific category \
from zero information would make someone's spending breakdown wrong, \
which is worse than leaving it blank for them to fill in themselves.
"""

_CATEGORIZE_TOOL = {
    "name": "assign_categories",
    "description": (
        "Assign a category to each transaction, in the same order given, "
        "or null for any with no real signal to go on."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "categories": {
                "type": "array",
                "description": "Same length and order as the input list.",
                "items": {
                    "type": ["string", "null"],
                    "enum": [key for key in TRANSACTION_CATEGORIES if key != "income"] + [None],
                },
            },
        },
        "required": ["categories"],
    },
}


# ---------------------------------------------------------------------------
# Anthropic client — lazy singleton
# ---------------------------------------------------------------------------

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    """Return the shared Anthropic client, initializing on first call.

    Explicit timeout: the SDK's own default is 10 minutes. On a deploy with
    only one or two sync Gunicorn workers, a single call that stalls that
    long (a network blip, an Anthropic-side slowdown) ties up a worker for
    the whole 10 minutes — during which every other request, from any user,
    just queues behind it with no free worker to answer. max_tokens here is
    only 1024, so a real reply normally finishes in a few seconds; a much
    shorter timeout lets a stalled call fail fast into the existing
    APITimeoutError handling (a clean "Request timed out" to the user)
    instead of quietly freezing the whole app for everyone.
    """
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. "
                "Add it in Render → Environment Variables."
            )
        timeout_seconds = float(os.environ.get("ARTHA_AI_TIMEOUT_SECONDS", "45"))
        _client = Anthropic(api_key=api_key, timeout=timeout_seconds)
        log.info(
            "Anthropic client initialized (model=%s, timeout=%ss).",
            _get_model(), timeout_seconds,
        )
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

    # This-month-by-category, using the *user's* local month boundary
    # (user_now, not a bare UTC/server date.today()) so "this month" agrees
    # with what the Finance page itself would show them right now — without
    # this the model can only eyeball category totals from the flat list
    # above, which is slow and error-prone even for a strong model.
    now = user_now(user)
    month_start = datetime(now.year, now.month, 1)
    next_month_start = (
        datetime(now.year + 1, 1, 1) if now.month == 12
        else datetime(now.year, now.month + 1, 1)
    )
    this_month_expenses = [
        t for t in expense_txs
        if t.timestamp and month_start <= t.timestamp.replace(tzinfo=None) < next_month_start
    ]
    if this_month_expenses:
        totals_by_category: dict[str, Decimal] = {}
        for t in this_month_expenses:
            key = t.category or "uncategorized"
            totals_by_category[key] = totals_by_category.get(key, zero) + t.amount
        lines += ["", f"This month's spending by category (as of {now.strftime('%b %d')}):"]
        for key, amount in sorted(totals_by_category.items(), key=lambda kv: kv[1], reverse=True):
            label = TRANSACTION_CATEGORIES.get(key, {}).get("label", key.title())
            lines.append(f"  {label}: ${amount:,.2f}")

    return "\n".join(lines)


def _assemble_budget_context(user) -> str:
    """Overall + per-category monthly caps, each against what's actually
    been spent so far this month — so the model can answer "am I over
    budget" instead of just knowing a cap exists with nothing to compare
    it to."""
    now = user_now(user)
    month_start = datetime(now.year, now.month, 1)
    next_month_start = (
        datetime(now.year + 1, 1, 1) if now.month == 12
        else datetime(now.year, now.month + 1, 1)
    )
    this_month_expenses = Transaction.query.filter(
        Transaction.user_id == user.id,
        Transaction.type == "expense",
        Transaction.timestamp >= month_start,
        Transaction.timestamp < next_month_start,
    ).all()
    spent_by_category: dict[str, Decimal] = {}
    total_spent = Decimal("0")
    for t in this_month_expenses:
        key = t.category or "uncategorized"
        spent_by_category[key] = spent_by_category.get(key, Decimal("0")) + t.amount
        total_spent += t.amount

    lines = []

    overall = Budget.query.filter_by(user_id=user.id).first()
    if overall and overall.monthly_cap > 0:
        lines.append(
            f"Overall monthly budget: ${overall.monthly_cap:,.2f}, "
            f"${total_spent:,.2f} spent so far this month "
            f"({total_spent / overall.monthly_cap * 100:.0f}%)."
        )

    category_budgets = CategoryBudget.query.filter_by(user_id=user.id).all()
    for cb in category_budgets:
        label = TRANSACTION_CATEGORIES.get(cb.category, {}).get("label", cb.category.title())
        spent = spent_by_category.get(cb.category, Decimal("0"))
        pct = (spent / cb.monthly_cap * 100) if cb.monthly_cap else Decimal("0")
        lines.append(f"{label} budget: ${cb.monthly_cap:,.2f}, ${spent:,.2f} spent ({pct:.0f}%).")

    return "\n".join(lines) if lines else "No budgets set."


def _assemble_upcoming_context(user) -> str:
    """Notes due soon and calendar events coming up — the same "what's
    coming due" question the dashboard already answers, just made
    available to the model too instead of only living in a different tab
    it has no visibility into."""
    now = user_now(user)
    today = now.date()

    lines = []

    notes_due_soon = (
        Note.query.filter(
            Note.user_id == user.id,
            Note.archived.is_(False),
            Note.deleted_at.is_(None),
            Note.due_date.isnot(None),
            Note.due_date >= today,
            Note.due_date <= today + timedelta(days=14),
        )
        .order_by(Note.due_date.asc())
        .all()
    )
    if notes_due_soon:
        lines.append("Notes due in the next 14 days:")
        for n in notes_due_soon:
            title = n.title or (n.preview[:40] if n.preview else "Untitled note")
            lines.append(f"  {n.due_date.strftime('%b %d')}: {title}")

    window_end = datetime(today.year, today.month, today.day) + timedelta(days=7)
    events_soon = (
        Event.query.filter(
            Event.user_id == user.id,
            Event.start >= datetime(today.year, today.month, today.day),
            Event.start < window_end,
        )
        .order_by(Event.start.asc())
        .all()
    )
    if events_soon:
        if lines:
            lines.append("")
        lines.append("Calendar events in the next 7 days:")
        for e in events_soon:
            lines.append(f"  {e.start.strftime('%b %d %I:%M %p')}: {e.title}")

    return "\n".join(lines) if lines else "Nothing due or scheduled in the near future."


def _assemble_scenario_context(user) -> str:
    """Active what-if scenarios, so a question like "should I move" can
    reference the one the user already modeled instead of starting from
    nothing."""
    active = (
        Scenario.query.filter_by(user_id=user.id, status="active")
        .order_by(Scenario.created_at.desc())
        .all()
    )
    if not active:
        return "No active scenarios."

    lines = []
    for s in active:
        net_monthly = s.monthly_savings - s.monthly_cost
        lines.append(
            f"{s.title} ({s.category}): one-time ${s.one_time_cost:,.2f}, "
            f"net ${net_monthly:,.2f}/mo, priority {s.priority}."
        )
    return "\n".join(lines)


def _build_system_prompt(user) -> str:
    first_name        = user.first_name or user.username
    # The user's own local date, not the server's — otherwise "tomorrow"
    # can resolve a full day off for anyone not physically in UTC (see
    # utils.user_now()'s docstring for exactly how that drifts).
    today             = user_now(user).strftime("%A, %B %d, %Y")
    financial_context = _assemble_financial_context(user)
    budget_context    = _assemble_budget_context(user)
    upcoming_context  = _assemble_upcoming_context(user)
    scenario_context  = _assemble_scenario_context(user)
    return _SYSTEM_PROMPT_TEMPLATE.format(
        first_name=first_name,
        today=today,
        financial_context=financial_context,
        budget_context=budget_context,
        upcoming_context=upcoming_context,
        scenario_context=scenario_context,
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

    # How many prior messages the route layer should load from the
    # Conversation/Message tables before calling chat()/stream_chat() —
    # same cap _sanitize_history() applies, exposed here so
    # blueprints/ai/routes.py has one source of truth for it instead of
    # a second hardcoded number.
    MAX_HISTORY_MESSAGES = _MAX_HISTORY_TURNS * 2

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

    # ------------------------------------------------------------------
    # Statement-PDF extraction  (fallback for the deterministic PDF parser)
    # ------------------------------------------------------------------

    @classmethod
    def extract_pdf_transactions(cls, pages_text: list[str]) -> dict:
        """
        Last-resort transaction extraction for a statement PDF the
        deterministic line-regex parser (_parse_statement_pdf in
        blueprints/finance/routes.py) couldn't read at all — called only
        when that parser finds zero rows, not as the default path, so
        this costs nothing for the statements the regex already handles.

        No user object / financial context needed: this is a pure
        text-in, transactions-out extraction, unrelated to the
        conversational assistant's own system prompt.

        Args:
            pages_text: One string per PDF page, as returned by
                        pdfplumber's page.extract_text().

        Returns:
            {"transactions": [{"date": "YYYY-MM-DD", "description": str,
                                "amount": float, "type": "income"|"expense"}]}
            {"error": str}
        """
        full_text = "\n".join(pages_text).strip()
        if not full_text:
            return {"transactions": []}

        truncated = len(full_text) > _STATEMENT_MAX_CHARS
        if truncated:
            full_text = full_text[:_STATEMENT_MAX_CHARS]

        try:
            client = _get_client()
        except RuntimeError as exc:
            log.error("AI client init failed for PDF extraction: %s", exc)
            return {"error": str(exc)}

        try:
            resp = client.messages.create(
                model=_get_model(),
                max_tokens=_STATEMENT_MAX_TOKENS,
                system=_STATEMENT_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": full_text}],
                tools=[_STATEMENT_TOOL],
                tool_choice={"type": "tool", "name": "record_transactions"},
            )
        except APITimeoutError:
            log.warning("Anthropic timeout during PDF statement extraction.")
            return {"error": "Request timed out. Please try again."}
        except APIConnectionError as exc:
            log.error("Anthropic connection error during PDF extraction: %s", exc)
            return {"error": "Could not reach AI service. Check connectivity."}
        except APIStatusError as exc:
            log.error("Anthropic status error %s during PDF extraction: %s", exc.status_code, exc.message)
            return {"error": f"AI service error ({exc.status_code}). Please try again."}
        except Exception as exc:
            log.exception("Unexpected error during PDF statement extraction: %s", exc)
            return {"error": "An unexpected error occurred."}

        tool_call = next(
            (block for block in resp.content if block.type == "tool_use"), None
        )
        transactions = tool_call.input.get("transactions", []) if tool_call else []

        return {"transactions": transactions, "truncated": truncated}

    # ------------------------------------------------------------------
    # Import categorization  (fallback for whatever _guess_category missed)
    # ------------------------------------------------------------------

    @classmethod
    def categorize_transactions(cls, descriptions: list[str]) -> dict:
        """
        Best-effort category for each description, called from
        blueprints/finance/routes.py's import_preview() only for the rows
        _guess_category's keyword match already left uncategorized — same
        "don't cost anything for the common case" shape as
        extract_pdf_transactions.

        Args:
            descriptions: Transaction descriptions, in order. Capped by
                          the caller (see _CATEGORIZE_MAX_ITEMS) — a very
                          long statement's tail just stays uncategorized
                          rather than this call growing unbounded.

        Returns:
            {"categories": [str | None, ...]} — same length and order as
            `descriptions`; None where the model had nothing real to go
            on (see _CATEGORIZE_SYSTEM_PROMPT for what that means).
            {"error": str}
        """
        if not descriptions:
            return {"categories": []}

        try:
            client = _get_client()
        except RuntimeError as exc:
            log.error("AI client init failed for categorization: %s", exc)
            return {"error": str(exc)}

        category_list = ", ".join(k for k in TRANSACTION_CATEGORIES if k != "income")
        numbered = "\n".join(f"{i + 1}. {d}" for i, d in enumerate(descriptions))

        try:
            resp = client.messages.create(
                model=_get_model(),
                max_tokens=_CATEGORIZE_MAX_TOKENS,
                system=_CATEGORIZE_SYSTEM_PROMPT.format(categories=category_list),
                messages=[{"role": "user", "content": numbered}],
                tools=[_CATEGORIZE_TOOL],
                tool_choice={"type": "tool", "name": "assign_categories"},
            )
        except APITimeoutError:
            log.warning("Anthropic timeout during import categorization.")
            return {"error": "Request timed out. Please try again."}
        except APIConnectionError as exc:
            log.error("Anthropic connection error during categorization: %s", exc)
            return {"error": "Could not reach AI service. Check connectivity."}
        except APIStatusError as exc:
            log.error("Anthropic status error %s during categorization: %s", exc.status_code, exc.message)
            return {"error": f"AI service error ({exc.status_code}). Please try again."}
        except Exception as exc:
            log.exception("Unexpected error during import categorization: %s", exc)
            return {"error": "An unexpected error occurred."}

        tool_call = next(
            (block for block in resp.content if block.type == "tool_use"), None
        )
        categories = tool_call.input.get("categories", []) if tool_call else []

        # A misaligned-length response can't be trusted to line up with the
        # input by position — safer to return nothing than risk silently
        # mislabeling one transaction as another's category.
        if len(categories) != len(descriptions):
            log.warning(
                "Categorization response length mismatch: got %d for %d descriptions.",
                len(categories), len(descriptions),
            )
            return {"categories": [None] * len(descriptions)}

        valid = set(TRANSACTION_CATEGORIES) - {"income"}
        return {"categories": [c if c in valid else None for c in categories]}
