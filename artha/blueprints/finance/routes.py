import calendar
import csv
import io
import math
import re
import time
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from flask import render_template, redirect, url_for, request, flash, session, jsonify, Response
from flask_login import login_required, current_user
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from ...extensions import db
from ...models import Transaction
from ...models.budget import Budget
from ...models.category_budget import CategoryBudget
from ...utils import is_ajax_request, current_month_bounds, budget_status, next_due_date
from . import finance_bp

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Categories — a small, closed set (not user-extensible). Deliberately kept
# to ~10 buckets: "broad categories that match real decisions" is the
# researched sweet spot for a personal budget — enough to be useful, few
# enough that categorizing a transaction is never a chore. A custom/
# per-user category system is a materially bigger feature (its own CRUD,
# migration of existing data, etc.) and isn't what was asked for here.
# ---------------------------------------------------------------------------

TRANSACTION_CATEGORIES = {
    "income":        {"label": "Income",             "icon": "trending-up",     "color": "#10b981"},
    "housing":       {"label": "Housing",             "icon": "home",            "color": "#6366f1"},
    "utilities":     {"label": "Utilities",           "icon": "zap",             "color": "#f59e0b"},
    "groceries":     {"label": "Groceries",           "icon": "shopping-cart",   "color": "#22c55e"},
    "dining":        {"label": "Dining",              "icon": "utensils",        "color": "#f97316"},
    "transport":     {"label": "Transport",           "icon": "car",             "color": "#0ea5e9"},
    "subscriptions": {"label": "Subscriptions",       "icon": "repeat",          "color": "#a855f7"},
    "shopping":      {"label": "Shopping",            "icon": "shopping-bag",    "color": "#ec4899"},
    "health":        {"label": "Health",              "icon": "heart-pulse",     "color": "#14b8a6"},
    "entertainment": {"label": "Entertainment",       "icon": "clapperboard",    "color": "#eab308"},
    "debt":          {"label": "Debt & loans",        "icon": "credit-card",     "color": "#ef4444"},
    "other":         {"label": "Other",               "icon": "more-horizontal", "color": "#64748b"},
}

# Not a real category — the bucket for expense/income rows with no category
# set at all, kept visually distinct (lighter gray) from the "Other" category
# above so users can tell "I chose Other" apart from "I never categorized this".
_UNCATEGORIZED = {"label": "Uncategorized", "icon": "help-circle", "color": "#94a3b8"}

# Keyword -> category, checked against a lowercased transaction description.
# Deliberately simple substring matching, not an ML/LLM call: it's free,
# instant, fully offline, and good enough for the common-merchant case that
# dominates a real bank statement. Order matters within a description only
# in the pathological case of two keywords both matching — first match in
# dict-iteration (i.e. definition) order wins, so more distinctive brand
# names are listed ahead of generic terms where that could matter.
_CATEGORY_KEYWORDS = {
    "housing": ["rent", "mortgage", "landlord", "property management"],
    "utilities": [
        "electric", "electricity", "water bill", "gas bill", "internet",
        "comcast", "xfinity", "verizon", "at&t", "att bill", "t-mobile",
        "utility", "utilities",
    ],
    "groceries": [
        "grocery", "groceries", "supermarket", "walmart", "target",
        "safeway", "kroger", "whole foods", "trader joe", "costco",
        "aldi", "publix",
    ],
    "dining": [
        "restaurant", "starbucks", "coffee", "mcdonald", "chipotle",
        "doordash", "uber eats", "ubereats", "grubhub", "pizza", "cafe",
        "diner", "bar & grill",
    ],
    "transport": [
        "uber", "lyft", "gas station", "shell", "chevron", "exxon",
        "parking", "transit", "metro", "dmv", "auto insurance",
    ],
    "subscriptions": [
        "netflix", "spotify", "hulu", "disney+", "disney plus",
        "amazon prime", "subscription", "apple.com/bill", "icloud",
        "youtube premium", "playstation plus", "xbox game pass",
    ],
    "shopping": ["amazon", "ebay", "best buy", "clothing", "mall", "ikea"],
    "health": [
        "pharmacy", "cvs", "walgreens", "doctor", "clinic", "hospital",
        "dental", "vision", "urgent care",
    ],
    "entertainment": [
        "movie", "cinema", "amc", "concert", "ticketmaster", "steam",
        "playstation store", "xbox live",
    ],
    "debt": ["loan payment", "credit card payment", "student loan"],
}


def _guess_category(description: str, t_type: str) -> str | None:
    """Best-effort category from a free-text description — used to
    pre-fill CSV-import rows so most of a statement doesn't need manual
    categorizing. Returns None (not "other") when nothing matches, so the
    caller can tell "confidently uncategorized" apart from "no guess"."""
    if t_type == "income":
        return "income"
    desc = (description or "").lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in desc for kw in keywords):
            return category
    return None


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    pass


def _validate_amount(amount_str: str) -> Decimal:
    """Parse and validate a user-supplied amount string → Decimal."""
    try:
        amount = Decimal(str(amount_str))
    except InvalidOperation:
        raise ValidationError("Invalid amount format.")
    if amount < 0:
        raise ValidationError("Amount must be non-negative.")
    return amount


def _resolve_transaction_timestamp(date_str: str | None) -> datetime:
    """
    Parse an optional YYYY-MM-DD date string into a datetime pinned to noon
    UTC (avoids the transaction silently landing on the "wrong" calendar
    day near a midnight UTC boundary). Falls back to today — also pinned
    to noon UTC — if the string is missing or malformed.
    """
    date_str = (date_str or "").strip()
    parsed = None
    if date_str:
        try:
            parsed = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            parsed = None
    if parsed is None:
        parsed = date.today()
    return datetime(parsed.year, parsed.month, parsed.day, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@finance_bp.route("/add_transaction", methods=["POST"])
@login_required
def add_transaction():
    description = request.form.get("description", "").strip()
    amount_str = request.form.get("amount", "").strip()
    t_type = request.form.get("type", "").strip()

    if not description:
        msg = "Description is required."
        return (jsonify({"message": msg}), 400) if is_ajax_request() else (flash(msg, "error"), redirect(url_for("dashboard.index")))[1]

    try:
        amount = _validate_amount(amount_str)
    except ValidationError as exc:
        msg = str(exc)
        if is_ajax_request():
            return jsonify({"message": msg}), 400
        flash(msg, "error")
        return redirect(url_for("dashboard.index"))

    if t_type not in ("income", "expense"):
        msg = "Invalid transaction type."
        if is_ajax_request():
            return jsonify({"message": msg}), 400
        flash(msg, "error")
        return redirect(url_for("dashboard.index"))

    category = request.form.get("category") or None
    if category not in TRANSACTION_CATEGORIES:
        category = None

    max_pos = (
        db.session.query(func.max(Transaction.position))
        .filter_by(user_id=current_user.id)
        .scalar()
        or 0
    )
    new_tx = Transaction(
        description=description,
        amount=amount,
        type=t_type,
        user_id=current_user.id,
        position=int(max_pos) + 1,
        timestamp=_resolve_transaction_timestamp(request.form.get("date")),
        is_recurring=bool(request.form.get("is_recurring")),
        category=category,
        import_source="manual",
    )

    try:
        db.session.add(new_tx)
        db.session.commit()
        if is_ajax_request():
            return render_template(
                "partials/transaction_row.html",
                tx=new_tx,
                today_date=date.today().strftime("%Y-%m-%d"),
                categories=TRANSACTION_CATEGORIES,
            )
        flash("Transaction added!", "success")
        return redirect(url_for("dashboard.index"))
    except Exception as e:
        db.session.rollback()
        log.error("Error adding transaction: %s", e, exc_info=True)
        msg = "Error adding transaction"
        if is_ajax_request():
            return jsonify({"message": msg}), 500
        flash(msg, "error")
        return redirect(url_for("dashboard.index"))


@finance_bp.route("/update_transaction/<int:transaction_id>", methods=["POST"])
@login_required
def update_transaction(transaction_id):
    tx = db.session.get(Transaction, transaction_id)
    if tx is None:
        return jsonify({"message": "Not found"}), 404
    if tx.user_id != current_user.id:
        return jsonify({"message": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    desc = (data.get("description") or tx.description).strip()
    t_type = data.get("type") or tx.type

    try:
        amount = Decimal(str(data.get("amount", tx.amount)))
        if amount < 0:
            return jsonify({"message": "Amount must be non-negative."}), 400
    except InvalidOperation:
        return jsonify({"message": "Invalid amount format."}), 400

    if t_type not in ("income", "expense"):
        return jsonify({"message": "Invalid transaction type."}), 400

    tx.description = desc
    tx.amount = amount
    tx.type = t_type

    # Only touch the date if one was actually sent — an edit to just the
    # description/amount/type shouldn't silently reset the transaction to
    # today.
    date_str = (data.get("date") or "").strip()
    if date_str:
        tx.timestamp = _resolve_transaction_timestamp(date_str)

    # Same "only touch what was sent" rule as date — "category" being
    # absent from the payload (every other caller of this endpoint today)
    # must never silently clear an existing category.
    if "category" in data:
        category = data.get("category") or None
        tx.category = category if category in TRANSACTION_CATEGORIES else None

    try:
        db.session.commit()
        return jsonify({
            "message": "Transaction updated successfully",
            "date": tx.timestamp.strftime("%Y-%m-%d"),
            "date_label": tx.timestamp.strftime("%b %d, %Y"),
        })
    except Exception as e:
        db.session.rollback()
        log.error("Error updating transaction: %s", e, exc_info=True)
        return jsonify({"message": "Database error"}), 500


@finance_bp.route("/delete_transaction/<int:transaction_id>", methods=["POST"])
@login_required
def delete_transaction(transaction_id):
    tx = db.session.get(Transaction, transaction_id)
    if tx is None:
        if is_ajax_request():
            return jsonify({"message": "Not found"}), 404
        flash("Transaction not found", "error")
        return redirect(url_for("dashboard.index"))

    if tx.user_id != current_user.id:
        if is_ajax_request():
            return jsonify({"message": "Unauthorized"}), 403
        flash("Unauthorized", "error")
        return redirect(url_for("dashboard.index"))

    # Store as string — Decimal is not JSON-serialisable
    session["last_deleted_tx"] = {
        "user_id": tx.user_id,
        "description": tx.description,
        "amount": str(tx.amount),
        "type": tx.type,
        "position": int(tx.position or 0),
        "is_recurring": bool(tx.is_recurring),
        "category": tx.category,
        "import_source": tx.import_source,
        "timestamp": (
            tx.timestamp.replace(tzinfo=timezone.utc).isoformat()
            if tx.timestamp
            else datetime.now(timezone.utc).isoformat()
        ),
        "deleted_at": time.time(),
    }

    try:
        # Deleting a recurring transaction only removes this month's row —
        # generate_recurring() picks its template from the most recent
        # is_recurring=True row for this (description, type), so an older
        # row still flagged recurring would silently regenerate this one
        # right back on the next /finance load. Deleting is the only way
        # this UI offers to cancel a recurring series (there's no separate
        # "stop recurring" action), so it has to mean "stop recurring",
        # not just "remove this one instance" — clear the flag on every
        # other row sharing this key too.
        if tx.is_recurring:
            Transaction.query.filter(
                Transaction.user_id == tx.user_id,
                Transaction.description == tx.description,
                Transaction.type == tx.type,
                Transaction.id != tx.id,
            ).update({"is_recurring": False})
        db.session.delete(tx)
        db.session.commit()
        if is_ajax_request():
            return jsonify({"message": "Transaction deleted", "can_undo": True})
        flash("Transaction deleted!", "success")
        return redirect(url_for("dashboard.index"))
    except Exception as e:
        db.session.rollback()
        log.error("Error deleting transaction: %s", e, exc_info=True)
        if is_ajax_request():
            return jsonify({"message": "Error deleting transaction"}), 500
        flash("Error deleting transaction", "error")
        return redirect(url_for("dashboard.index"))


@finance_bp.route("/undo_delete_transaction", methods=["POST"])
@login_required
def undo_delete_transaction():
    data = session.get("last_deleted_tx")

    if not data or data.get("user_id") != current_user.id:
        return jsonify({"message": "Nothing to undo."}), 400

    if time.time() - float(data.get("deleted_at", 0)) > 10:
        session.pop("last_deleted_tx", None)
        return jsonify({"message": "Undo window expired."}), 400

    try:
        ts = None
        try:
            ts = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
        except Exception:
            ts = None

        restored_pos = int(data.get("position") or 0)
        if restored_pos <= 0:
            max_pos = (
                db.session.query(func.max(Transaction.position))
                .filter_by(user_id=current_user.id)
                .scalar()
                or 0
            )
            restored_pos = int(max_pos) + 1
        else:
            Transaction.query.filter(
                Transaction.user_id == current_user.id,
                Transaction.position >= restored_pos,
            ).update(
                {Transaction.position: Transaction.position + 1},
                synchronize_session=False,
            )

        restored = Transaction(
            description=data["description"],
            amount=Decimal(data["amount"]),
            type=data["type"],
            user_id=current_user.id,
            position=restored_pos,
            timestamp=ts or db.func.current_timestamp(),
            is_recurring=bool(data.get("is_recurring")),
            category=data.get("category"),
            import_source=data.get("import_source"),
        )
        db.session.add(restored)
        db.session.commit()
        session.pop("last_deleted_tx", None)

        row_html = render_template(
            "partials/transaction_row.html",
            tx=restored,
            today_date=date.today().strftime("%Y-%m-%d"),
            categories=TRANSACTION_CATEGORIES,
        )
        return jsonify({"message": "Transaction restored.", "row_html": row_html})
    except Exception as e:
        db.session.rollback()
        log.error("Error undoing delete: %s", e, exc_info=True)
        return jsonify({"message": "Error restoring transaction"}), 500


@finance_bp.route("/finance/transaction/<int:transaction_id>/toggle-recurring", methods=["PATCH"])
@login_required
def toggle_recurring(transaction_id):
    tx = db.session.get(Transaction, transaction_id)
    if tx is None:
        return jsonify({"message": "Not found"}), 404
    if tx.user_id != current_user.id:
        return jsonify({"message": "Unauthorized"}), 403

    turning_off = tx.is_recurring
    tx.is_recurring = not tx.is_recurring

    # Same reasoning as delete_transaction(): generate_recurring() picks
    # its template from the most recent is_recurring=True row for this
    # (description, type). If an older sibling row is still flagged
    # recurring, turning this one off wouldn't actually stop the series —
    # the next /finance load would just regenerate off that older row.
    if turning_off:
        Transaction.query.filter(
            Transaction.user_id == tx.user_id,
            Transaction.description == tx.description,
            Transaction.type == tx.type,
            Transaction.id != tx.id,
        ).update({"is_recurring": False})

    try:
        db.session.commit()
        return jsonify({"message": "Recurring status updated", "is_recurring": tx.is_recurring})
    except Exception as e:
        db.session.rollback()
        log.error("Error toggling recurring flag: %s", e, exc_info=True)
        return jsonify({"message": "Database error"}), 500


@finance_bp.route("/finance/generate-recurring", methods=["POST"])
@login_required
def generate_recurring():
    """
    Auto-generate this month's copy of every recurring transaction that
    doesn't already have one. Called silently on every /finance page load.

    A recurring transaction "already exists this month" if a transaction
    with the same description + type falls within the current calendar
    month — that's the dedup key, per spec.
    """
    uid = current_user.id
    today = date.today()
    month_start = _month_start(today.year, today.month)
    next_month_start = _month_start(
        today.year + 1 if today.month == 12 else today.year,
        1 if today.month == 12 else today.month + 1,
    )

    recurring_txs = Transaction.query.filter_by(user_id=uid, is_recurring=True).all()

    # Recurring transactions accumulate one row per month (each generated
    # copy stays is_recurring=True so it keeps showing the recurring UI).
    # Collapse to one representative row per unique (description, type) —
    # the most recent — so a "Netflix" template with 6 months of history
    # doesn't get processed 6 times.
    templates_by_key: dict[tuple[str, str], Transaction] = {}
    for tx in recurring_txs:
        key = (tx.description, tx.type)
        current = templates_by_key.get(key)
        if current is None or (tx.timestamp and current.timestamp and tx.timestamp > current.timestamp):
            templates_by_key[key] = tx

    existing_this_month = Transaction.query.filter(
        Transaction.user_id == uid,
        Transaction.timestamp >= month_start,
        Transaction.timestamp < next_month_start,
    ).all()
    existing_keys = {(t.description, t.type) for t in existing_this_month}

    max_pos = (
        db.session.query(func.max(Transaction.position))
        .filter_by(user_id=uid)
        .scalar()
        or 0
    )

    generated = 0
    skipped = 0

    for key, template_tx in templates_by_key.items():
        if key in existing_keys:
            skipped += 1
            continue

        # Preserve the template's own day-of-month (clamped to the current
        # month's length, e.g. day 31 in a 30-day month) rather than
        # stamping every generated copy with today's date — otherwise
        # every recurring bill without a copy yet this month piles onto
        # whatever day the user happens to next load /finance, instead of
        # landing on the day it's actually due. Same clamping already used
        # by the calendar's upcoming-recurring reminder (see
        # next_due_date() in artha/utils.py).
        days_this_month = calendar.monthrange(today.year, today.month)[1]
        target_day = min(template_tx.timestamp.day, days_this_month)
        target_date = date(today.year, today.month, target_day)

        max_pos += 1
        new_tx = Transaction(
            description=template_tx.description,
            amount=template_tx.amount,
            type=template_tx.type,
            user_id=uid,
            position=int(max_pos),
            is_recurring=True,
            timestamp=_resolve_transaction_timestamp(target_date.strftime("%Y-%m-%d")),
            recurring_month=month_start,
        )

        # Committed one at a time (inside a savepoint) rather than as one
        # batch: the uq_transaction_recurring_month constraint is what
        # actually stops two near-simultaneous /finance loads from both
        # generating the same bill, but that only works if a losing
        # IntegrityError can be caught and skipped without discarding the
        # other, unrelated templates already generated in this same pass.
        try:
            with db.session.begin_nested():
                db.session.add(new_tx)
            generated += 1
        except IntegrityError:
            db.session.rollback()
            skipped += 1

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        log.error("Error generating recurring transactions: %s", e, exc_info=True)
        return jsonify({"message": "Error generating recurring transactions"}), 500

    return jsonify({"generated": generated, "skipped": skipped})


@finance_bp.get("/api/finance_totals")
@login_required
def finance_totals():
    """
    Direct DB query — the in-memory cache has been removed.

    Why: the old `finance_cache = {}` was a module-level dict that breaks
    under Gunicorn multi-worker deployments (each worker has its own copy).
    A single PostgreSQL aggregate query is fast enough for one user's data
    and is always correct across all workers.

    Scoped to the current month, same as the dashboard route that renders
    the cards this endpoint refreshes — this is only ever called to
    live-update the dashboard's stat cards/chart after a transaction is
    added/edited/deleted, so it needs to match what the page just showed
    on load, not sum all-time totals back in.
    """
    uid = current_user.id
    month_start, month_end = current_month_bounds()

    income = (
        db.session.query(func.sum(Transaction.amount))
        .filter(
            Transaction.user_id == uid,
            Transaction.type == "income",
            Transaction.timestamp >= month_start,
            Transaction.timestamp < month_end,
        )
        .scalar()
        or Decimal("0")
    )
    expense = (
        db.session.query(func.sum(Transaction.amount))
        .filter(
            Transaction.user_id == uid,
            Transaction.type == "expense",
            Transaction.timestamp >= month_start,
            Transaction.timestamp < month_end,
        )
        .scalar()
        or Decimal("0")
    )
    balance = income - expense

    return jsonify({
        "income": float(income),
        "expense": float(expense),
        "balance": float(balance),
    })


# ---------------------------------------------------------------------------
# Monthly Tabs — full finance page with month-by-month filtering
# ---------------------------------------------------------------------------

def _month_start(year: int, month: int) -> date:
    return date(year, month, 1)


def _prev_month_start(d: date) -> date:
    last_day_of_prev = _month_start(d.year, d.month) - timedelta(days=1)
    return _month_start(last_day_of_prev.year, last_day_of_prev.month)


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _recurring_bills(uid: int, today: date) -> list[dict]:
    """Every distinct recurring "rule" (by description + type), soonest due
    first — shared by finance_page()'s panel and the Recurring tab's
    breakdown so both agree on the exact same list. Not a raw row count:
    each rule accumulates one generated row per month, so the most recent
    row per rule is the template used both for its displayed amount/
    category and for next_due_date()'s day-of-month signal — same
    dedup-then-next_due_date() pattern the dashboard's "renewals this
    week" and the calendar's upcoming-recurring banner already use, so
    all agree on the same due date for the same bill.
    """
    recurring_rows = Transaction.query.filter_by(user_id=uid, is_recurring=True).all()
    templates: dict[tuple[str, str], Transaction] = {}
    for t in recurring_rows:
        key = (t.description, t.type)
        current = templates.get(key)
        if current is None or (t.timestamp and current.timestamp and t.timestamp > current.timestamp):
            templates[key] = t

    bills = []
    for (desc, ttype), tx in templates.items():
        due = next_due_date(tx, today)
        bills.append({
            "description": desc,
            "type": ttype,
            "amount": float(tx.amount),
            "category": tx.category,
            "due_date": due,
            "due_label": (
                "Today" if due == today
                else f"{calendar.month_abbr[due.month]} {due.day}" if due
                else None
            ),
            # Matches the dashboard's "renewals this week" and the
            # calendar's upcoming-recurring banner — same 7-day window
            # counts as "soon" everywhere it's shown in the app.
            "due_soon": due is not None and 0 <= (due - today).days <= 7,
        })
    # Soonest due first; a rule with no resolvable due date (shouldn't
    # happen in practice — next_due_date always finds one within a year)
    # sorts last rather than crashing the comparison.
    bills.sort(key=lambda b: b["due_date"] or date.max)
    return bills


@finance_bp.route("/finance")
@login_required
def finance_page():
    """
    Full finance page with month-by-month filtering.

    Query param:
        ?month=YYYY-MM  — show that month only
        ?month=all      — show all-time (unfiltered), like the old view
        (none)          — defaults to the current month
    """
    uid = current_user.id
    all_tx = (
        Transaction.query.filter_by(user_id=uid)
        .order_by(Transaction.timestamp.asc(), Transaction.id.asc())
        .all()
    )

    today = date.today()
    month_param = (request.args.get("month") or "").strip()
    all_time = month_param == "all"

    if not all_time and month_param:
        try:
            sel_year, sel_month = (int(part) for part in month_param.split("-", 1))
            selected_date = _month_start(sel_year, sel_month)
        except (ValueError, TypeError):
            selected_date = _month_start(today.year, today.month)
    else:
        selected_date = _month_start(today.year, today.month)

    # Bucket every transaction by "YYYY-MM" once, rather than re-scanning
    # the full list for every month we need totals for.
    buckets = defaultdict(lambda: {"income": Decimal("0"), "expense": Decimal("0"), "txs": []})
    for tx in all_tx:
        if not tx.timestamp:
            continue
        key = tx.timestamp.strftime("%Y-%m")
        bucket = buckets[key]
        bucket["txs"].append(tx)
        if tx.type == "income":
            bucket["income"] += tx.amount
        elif tx.type == "expense":
            bucket["expense"] += tx.amount

    def bucket_for(d: date) -> dict:
        return buckets.get(d.strftime("%Y-%m"), {"income": Decimal("0"), "expense": Decimal("0"), "txs": []})

    # Last 12 months (oldest -> newest, ending at the current month) for the tab row.
    last_12 = []
    cursor_year, cursor_month = today.year, today.month
    for _ in range(12):
        last_12.append(_month_start(cursor_year, cursor_month))
        cursor_month -= 1
        if cursor_month == 0:
            cursor_month = 12
            cursor_year -= 1
    last_12.reverse()

    month_tabs = []
    for d in last_12:
        b = bucket_for(d)
        month_tabs.append({
            "value": d.strftime("%Y-%m"),
            "label": f"{calendar.month_abbr[d.month]} {d.year}",
            "net": float(b["income"] - b["expense"]),
            "is_current": (d.year == today.year and d.month == today.month),
        })

    if all_time:
        transactions = all_tx
        income = sum((t.amount for t in all_tx if t.type == "income"), Decimal("0"))
        expense = sum((t.amount for t in all_tx if t.type == "expense"), Decimal("0"))
        selected_month_value = "all"
        selected_month_label = "All time"
    else:
        b = bucket_for(selected_date)
        transactions = b["txs"]
        income = b["income"]
        expense = b["expense"]
        selected_month_value = selected_date.strftime("%Y-%m")
        selected_month_label = f"{calendar.month_name[selected_date.month]} {selected_date.year}"

    balance = income - expense

    # Comparison vs. the previous month — meaningless for "all time".
    comparison = None
    if not all_time:
        prev_bucket = bucket_for(_prev_month_start(selected_date))
        prev_income = prev_bucket["income"]
        prev_expense = prev_bucket["expense"]
        prev_balance = prev_income - prev_expense

        def _cmp(curr: Decimal, prev: Decimal, higher_is_better: bool) -> dict:
            delta = curr - prev
            up = delta >= 0
            favorable = up if higher_is_better else not up
            return {"delta": float(abs(delta)), "up": up, "favorable": favorable}

        comparison = {
            "income": _cmp(income, prev_income, True),
            "expense": _cmp(expense, prev_expense, False),
            "net": _cmp(balance, prev_balance, True),
        }

    savings_rate = float((balance / income) * 100) if income > 0 else 0.0
    # Ring geometry for the Savings Rate gauge — radius matches the SVG in
    # finance.html (viewBox 88x88, r=38). Clamp to [0, 100] for the visual
    # fill; the raw (possibly negative or >100) figure is still what's shown
    # as text next to it.
    _ring_circumference = 2 * math.pi * 38
    savings_ring_offset = _ring_circumference * (1 - max(0.0, min(100.0, savings_rate)) / 100)

    # Biggest expense "category" and the single day of the month with the
    # most spending. Prefers the real category field where a transaction
    # has one (labeled via TRANSACTION_CATEGORIES); falls back to the old
    # first-word-of-description heuristic only for the uncategorized
    # remainder, so pre-existing data (and anything a user never bothers
    # to categorize) still produces a sensible stat instead of nothing.
    expense_txs = [t for t in transactions if t.type == "expense"] if not all_time else [
        t for t in all_tx if t.type == "expense"
    ]

    category_totals: dict[str, Decimal] = defaultdict(Decimal)
    day_totals: dict[int, Decimal] = defaultdict(Decimal)
    for t in expense_txs:
        if t.category and t.category in TRANSACTION_CATEGORIES:
            label = TRANSACTION_CATEGORIES[t.category]["label"]
        else:
            first_word = (t.description or "").strip().split(" ")[0] if (t.description or "").strip() else "Other"
            label = first_word.capitalize()
        category_totals[label] += t.amount
        if t.timestamp:
            day_totals[t.timestamp.day] += t.amount

    biggest_category = max(category_totals.items(), key=lambda kv: kv[1])[0] if category_totals else None
    biggest_day = max(day_totals.items(), key=lambda kv: kv[1])[0] if day_totals else None
    biggest_day_label = f"The {_ordinal(biggest_day)}" if biggest_day else None

    # 6-month trend for the bar chart (oldest -> newest). Trim any leading
    # months with no transactions so a new user with 1-2 months of history
    # doesn't see 4-5 empty bars — but still cap the window at 6 months.
    last_6 = last_12[-6:]
    months_with_data = [i for i, d in enumerate(last_6) if bucket_for(d)["txs"]]
    trend_start = months_with_data[0] if months_with_data else len(last_6) - 1
    trend_months = last_6[trend_start:]

    trend_data = []
    for d in trend_months:
        b = bucket_for(d)
        trend_data.append({
            "value": d.strftime("%Y-%m"),
            "label": f"{calendar.month_abbr[d.month]} {d.year}",
            "net": float(b["income"] - b["expense"]),
        })

    recurring_bills = _recurring_bills(uid, today)
    recurring_count = len(recurring_bills)

    # Always the real current month's spend, independent of whatever month
    # is being browsed above — "my budget" means this calendar month, not
    # whichever one the filter tabs happen to be showing.
    budget_row = Budget.query.filter_by(user_id=uid).first()
    current_month_txs = bucket_for(_month_start(today.year, today.month))["txs"]
    current_month_expense = bucket_for(_month_start(today.year, today.month))["expense"]
    budget = budget_status(budget_row.monthly_cap if budget_row else None, current_month_expense)

    # Per-category budgets: same current-month scope as the overall budget
    # above, just grouped by the raw category slug instead of summed.
    category_expense_totals: dict[str, Decimal] = defaultdict(Decimal)
    for t in current_month_txs:
        if t.type == "expense" and t.category:
            category_expense_totals[t.category] += t.amount

    category_budget_rows = CategoryBudget.query.filter_by(user_id=uid).all()
    category_budgets = [
        {
            "category": row.category,
            "label": TRANSACTION_CATEGORIES.get(row.category, {}).get("label", row.category),
            "icon": TRANSACTION_CATEGORIES.get(row.category, {}).get("icon"),
            "status": budget_status(row.monthly_cap, category_expense_totals.get(row.category, Decimal("0"))),
        }
        for row in category_budget_rows
    ]
    budgeted_categories = {row.category for row in category_budget_rows}
    budgetable_categories = {
        key: val for key, val in TRANSACTION_CATEGORIES.items()
        if key != "income" and key not in budgeted_categories
    }

    # Every calendar year that has at least one transaction, newest first —
    # populates the year picker on the Cash Flow/Spending/Income tabs so
    # users can look back at last year's (or any past year's) totals, not
    # just rolling trailing windows anchored to today.
    available_years = sorted({t.timestamp.year for t in all_tx if t.timestamp}, reverse=True)
    if today.year not in available_years:
        available_years.insert(0, today.year)

    # Every calendar month that has at least one transaction, newest first —
    # the month picker shown when "Month" is the selected period on the
    # Cash Flow/Spending/Income tabs, so a user can drill into e.g. March
    # 2026 specifically rather than only ever seeing whichever month is
    # "current". Reuses `buckets` (already keyed by "YYYY-MM" from the loop
    # above) instead of re-scanning all_tx a second time.
    current_month_key = today.strftime("%Y-%m")
    available_months = [
        {"value": key, "label": f"{calendar.month_abbr[int(key[5:7])]} {key[:4]}"}
        for key in sorted(buckets.keys(), reverse=True)
    ]
    if not available_months or available_months[0]["value"] != current_month_key:
        available_months.insert(0, {"value": current_month_key, "label": f"{calendar.month_abbr[today.month]} {today.year}"})

    return render_template(
        "finance.html",
        transactions=transactions,
        income=float(income),
        expense=float(expense),
        balance=float(balance),
        month_tabs=month_tabs,
        selected_month_value=selected_month_value,
        selected_month_label=selected_month_label,
        all_time=all_time,
        comparison=comparison,
        savings_rate=savings_rate,
        savings_ring_offset=savings_ring_offset,
        biggest_category=biggest_category,
        biggest_day_label=biggest_day_label,
        trend_data=trend_data,
        recurring_count=recurring_count,
        recurring_bills=recurring_bills,
        budget=budget,
        budget_cap_raw=(budget_row.monthly_cap if budget_row else None),
        category_budgets=category_budgets,
        budgetable_categories=budgetable_categories,
        today_date=today.strftime("%Y-%m-%d"),
        categories=TRANSACTION_CATEGORIES,
        available_years=available_years,
        available_months=available_months,
    )


# ---------------------------------------------------------------------------
# Cash Flow / Spending / Income tabs — period-flexible aggregation, fetched
# on demand (JSON) so switching tabs or periods never reloads the page.
# Three "views" share one endpoint since they all start from the same
# date-bounded transaction query:
#   - cashflow: income vs. expense per month bucket within the period
#   - spending: expense total broken down by category
#   - income:   income total broken down by category
# ---------------------------------------------------------------------------

def _period_bounds(period: str, year: int, today: date, month_str: str | None = None) -> tuple[date, date, str]:
    """(start, end_exclusive, label) for a period key.

    "3m"/"6m"/"12m" are trailing windows anchored to today (matches how
    banking apps show "Last 3 Months" — always relative to now, not to
    whatever year is selected). "year" reads the `year` param, and "month"
    reads `month_str` ("YYYY-MM") — the two periods that let a user look
    back at a specific past year or a specific past month rather than only
    ever seeing today's.
    """
    if period == "month":
        target = _month_start(today.year, today.month)
        if month_str:
            try:
                m_year, m_month = (int(part) for part in month_str.split("-", 1))
                target = _month_start(m_year, m_month)
            except (ValueError, TypeError):
                pass
        start = target
        end = _month_start(target.year + 1, 1) if target.month == 12 else _month_start(target.year, target.month + 1)
        return start, end, f"{calendar.month_name[target.month]} {target.year}"

    if period in ("3m", "6m", "12m"):
        n = {"3m": 3, "6m": 6, "12m": 12}[period]
        end = _month_start(today.year + 1, 1) if today.month == 12 else _month_start(today.year, today.month + 1)
        cursor_year, cursor_month = today.year, today.month
        for _ in range(n - 1):
            cursor_month -= 1
            if cursor_month == 0:
                cursor_month = 12
                cursor_year -= 1
        start = _month_start(cursor_year, cursor_month)
        return start, end, f"Last {n} Months"

    # "year" (and any unrecognized value, so the route never 500s on a bad param).
    # A past year runs Jan-Dec in full; the current year stops at the end of
    # *this* month rather than running out to December, so "2026" doesn't
    # render 4 empty trailing months — it's year-to-date, same as every
    # other period here being "as of today" rather than a fixed future window.
    start = date(year, 1, 1)
    if year == today.year:
        end = _month_start(today.year + 1, 1) if today.month == 12 else _month_start(today.year, today.month + 1)
        label = f"{year} (Year to Date)"
    else:
        end = date(year + 1, 1, 1)
        label = str(year)
    return start, end, label


@finance_bp.route("/finance/breakdown")
@login_required
def finance_breakdown():
    uid = current_user.id
    view = (request.args.get("view") or "spending").strip().lower()
    period = (request.args.get("period") or "month").strip().lower()
    today = date.today()
    try:
        year = int(request.args.get("year") or today.year)
    except (TypeError, ValueError):
        year = today.year
    month_param = (request.args.get("month") or "").strip()

    if view not in ("spending", "income", "cashflow", "recurring"):
        view = "spending"
    if period not in ("month", "3m", "6m", "12m", "year"):
        period = "month"

    start, end, period_label = _period_bounds(period, year, today, month_param or None)

    if view == "recurring":
        # Recurring rules aren't bounded by a real transaction date range
        # the way the other three views are — there's nothing to query by
        # date, since a rule always describes "the current commitment,"
        # not a historical record. Instead the period picker here scales
        # a *projection*: the number of calendar months between `start`
        # and `end` (1 for a month, 3/6/12 for those windows, and — reusing
        # the exact same _period_bounds() "Year to Date" logic Cash Flow
        # already has — however many months have elapsed for the current
        # year, or a full 12 for a past one) multiplied by the monthly
        # commitment, so "Last 3 Months" reads as "3 months of this bill
        # load," consistent with what every other period label means
        # elsewhere on this page.
        months = (end.year - start.year) * 12 + (end.month - start.month)
        bills = _recurring_bills(uid, today)
        monthly_income = sum(b["amount"] for b in bills if b["type"] == "income")
        monthly_expense = sum(b["amount"] for b in bills if b["type"] == "expense")
        items = [
            {
                "description": b["description"],
                "type": b["type"],
                "amount": b["amount"],
                "category": b["category"],
                "due_label": b["due_label"],
                "due_soon": b["due_soon"],
            }
            for b in bills
        ]
        return jsonify({
            "view": view,
            "period": period,
            "period_label": period_label,
            "months": months,
            "monthly_income": monthly_income,
            "monthly_expense": monthly_expense,
            "income_total": monthly_income * months,
            "expense_total": monthly_expense * months,
            "net": (monthly_income - monthly_expense) * months,
            "items": items,
        })

    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end_dt = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)

    txs = Transaction.query.filter(
        Transaction.user_id == uid,
        Transaction.timestamp >= start_dt,
        Transaction.timestamp < end_dt,
    ).all()

    if view == "cashflow":
        month_keys = []
        cursor_year, cursor_month = start.year, start.month
        while _month_start(cursor_year, cursor_month) < end:
            month_keys.append(_month_start(cursor_year, cursor_month))
            cursor_month += 1
            if cursor_month == 13:
                cursor_month = 1
                cursor_year += 1

        bucket_totals = {d.strftime("%Y-%m"): {"income": Decimal("0"), "expense": Decimal("0")} for d in month_keys}
        for t in txs:
            if not t.timestamp:
                continue
            b = bucket_totals.get(t.timestamp.strftime("%Y-%m"))
            if b is None:
                continue
            if t.type == "income":
                b["income"] += t.amount
            elif t.type == "expense":
                b["expense"] += t.amount

        buckets = []
        income_total = Decimal("0")
        expense_total = Decimal("0")
        for d in month_keys:
            b = bucket_totals[d.strftime("%Y-%m")]
            income_total += b["income"]
            expense_total += b["expense"]
            buckets.append({
                "label": f"{calendar.month_abbr[d.month]} {d.year}" if len(month_keys) > 12 else calendar.month_abbr[d.month],
                "income": float(b["income"]),
                "expense": float(b["expense"]),
                "net": float(b["income"] - b["expense"]),
            })

        return jsonify({
            "view": view,
            "period": period,
            "period_label": period_label,
            "buckets": buckets,
            "income_total": float(income_total),
            "expense_total": float(expense_total),
            "net": float(income_total - expense_total),
        })

    # spending / income — same shape, just filtered to the opposite tx type.
    want_type = "expense" if view == "spending" else "income"
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for t in txs:
        if t.type != want_type:
            continue
        key = t.category if (t.category and t.category in TRANSACTION_CATEGORIES) else "_uncategorized"
        totals[key] += t.amount

    total = sum(totals.values(), Decimal("0"))
    categories = []
    for key, amount in sorted(totals.items(), key=lambda kv: kv[1], reverse=True):
        meta = _UNCATEGORIZED if key == "_uncategorized" else TRANSACTION_CATEGORIES[key]
        pct = float((amount / total) * 100) if total > 0 else 0.0
        categories.append({
            "key": key,
            "label": meta["label"],
            "icon": meta["icon"],
            "color": meta["color"],
            "amount": float(amount),
            "pct": pct,
        })

    return jsonify({
        "view": view,
        "period": period,
        "period_label": period_label,
        "total": float(total),
        "categories": categories,
    })


# ---------------------------------------------------------------------------
# CSV import — bank statement upload. One generic, bank-agnostic parser
# (not a per-bank integration) plus a client-side preview/edit step as the
# safety net for whatever the parser gets wrong, rather than trying to
# perfectly special-case every bank's export format up front.
# ---------------------------------------------------------------------------

_IMPORT_DATE_FORMATS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%m-%d-%Y",
    "%d-%m-%Y",
    "%b %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%d-%b-%Y",
    "%m/%d/%y",
    "%d/%m/%y",
]
# Same formats, slash/dash ones reordered day-first. A bare "03/04" is
# genuinely ambiguous (March 4 or April 3?) with nothing in the text to
# settle it — used instead of _IMPORT_DATE_FORMATS, whole-document, when
# _detect_day_first finds a currency symbol whose country overwhelmingly
# writes dates day-first (see that function).
_IMPORT_DATE_FORMATS_DAY_FIRST = [
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%m-%d-%Y",
    "%b %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%d-%b-%Y",
    "%d/%m/%y",
    "%m/%d/%y",
]

_DATE_HEADER_ALIASES = {"date", "transaction date", "posted date", "trans date", "post date", "value date"}
_DESC_HEADER_ALIASES = {"description", "memo", "narrative", "details", "payee", "merchant", "particulars"}
_AMOUNT_HEADER_ALIASES = {"amount", "transaction amount", "amt"}
_DEBIT_HEADER_ALIASES = {"debit", "withdrawal", "withdrawals", "money out", "dr"}
_CREDIT_HEADER_ALIASES = {"credit", "deposit", "deposits", "money in", "cr"}

# £ (UK), € (most of the Eurozone) and ৳ (Bangladesh) all overwhelmingly
# write dates day-first. Deliberately NOT keying off a bare "$" — that
# currency spans the US, Canada, Australia and others with no single
# dominant order, so a $-only statement keeps the existing US-first
# default rather than risk flipping it wrong for the far more common USD
# case.
_DAY_FIRST_CURRENCY_SYMBOLS = ("£", "€", "৳")


def _detect_day_first(text: str) -> bool:
    """Whole-document hint for which way to resolve an ambiguous slash
    date, based on which currency symbol the statement actually prints —
    the address/letterhead is not used for this because a statement can
    legitimately show two countries (e.g. a Bangladeshi bank statement
    mailed to a customer's US address), and the currency the transactions
    are actually denominated in is the reliable signal."""
    return any(sym in text for sym in _DAY_FIRST_CURRENCY_SYMBOLS)


def _parse_import_date(raw: str, day_first: bool = False) -> date | None:
    """
    Tries each statement date format banks commonly export in, in order.
    Ambiguous day/month ordering (e.g. "03/04") is resolved US-first by
    default, day-first when `day_first` is set (see _detect_day_first) —
    and the preview step (every parsed date is shown before anything is
    saved) is what catches anything still wrong, not this function.
    Critically, this NEVER falls back to today: a row whose date can't be
    parsed is skipped (see _parse_statement_csv), not silently dated to
    the day of the upload — a March statement imported in July must file
    under March, or the whole point of importing history is defeated.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    formats = _IMPORT_DATE_FORMATS_DAY_FIRST if day_first else _IMPORT_DATE_FORMATS
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _detect_import_columns(header: list[str]) -> dict:
    """Maps a CSV header row to column indices by role, matching common
    bank export header names case-insensitively. A role missing from the
    return dict means it wasn't found — the caller decides what's required."""
    normalized = [h.strip().lower() for h in header]
    roles: dict[str, int] = {}
    for i, h in enumerate(normalized):
        if h in _DATE_HEADER_ALIASES and "date" not in roles:
            roles["date"] = i
        elif h in _DESC_HEADER_ALIASES and "description" not in roles:
            roles["description"] = i
        elif h in _AMOUNT_HEADER_ALIASES and "amount" not in roles:
            roles["amount"] = i
        elif h in _DEBIT_HEADER_ALIASES and "debit" not in roles:
            roles["debit"] = i
        elif h in _CREDIT_HEADER_ALIASES and "credit" not in roles:
            roles["credit"] = i
    return roles


def _parse_import_amount(raw: str) -> Decimal | None:
    """Strips common statement formatting ($/£/€, commas, parens-for-negative)
    before parsing — a bank export rarely hands back a bare number."""
    raw = (raw or "").strip()
    if not raw:
        return None
    negative = raw.startswith("(") and raw.endswith(")")
    cleaned = raw.strip("()")
    for symbol in ("$", "£", "€", "৳", ","):
        cleaned = cleaned.replace(symbol, "")
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return -value if negative else value


def _parse_statement_csv(file_stream) -> tuple[list[dict], list[str]]:
    """
    Parses an uploaded bank-statement CSV into row dicts, each carrying
    its own statement date (see _parse_import_date's docstring on why
    that's never defaulted) and a best-effort category guess. Rows with
    an unparseable date/amount are excluded and reported as warnings
    rather than silently dropped or defaulted. Returns (rows, warnings);
    nothing is written to the database here.
    """
    try:
        text = file_stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            file_stream.seek(0)
            text = file_stream.read().decode("latin-1")
        except Exception:
            return [], ["Could not read file — please export as CSV (UTF-8 or Latin-1)."]

    day_first = _detect_day_first(text)

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return [], ["File is empty."]

    roles = _detect_import_columns(header)
    if "date" not in roles or "description" not in roles:
        return [], [
            "Couldn't find date and description columns. Expected headers like "
            "\"Date\", \"Description\", and \"Amount\" (or \"Debit\"/\"Credit\")."
        ]
    if "amount" not in roles and "debit" not in roles and "credit" not in roles:
        return [], ["Couldn't find an amount column (\"Amount\", or \"Debit\"/\"Credit\")."]

    warnings: list[str] = []
    rows: list[dict] = []
    for line_num, raw_row in enumerate(reader, start=2):
        if not raw_row or all(not cell.strip() for cell in raw_row):
            continue

        def cell(role: str) -> str:
            idx = roles.get(role)
            return raw_row[idx].strip() if idx is not None and idx < len(raw_row) else ""

        parsed_date = _parse_import_date(cell("date"), day_first=day_first)
        description = cell("description")
        if parsed_date is None or not description:
            warnings.append(f"Row {line_num}: skipped — missing/unparseable date or description.")
            continue

        if "debit" in roles or "credit" in roles:
            debit = _parse_import_amount(cell("debit"))
            credit = _parse_import_amount(cell("credit"))
            if debit:
                amount, t_type = abs(debit), "expense"
            elif credit:
                amount, t_type = abs(credit), "income"
            else:
                warnings.append(f"Row {line_num}: skipped — no debit or credit amount.")
                continue
        else:
            amount = _parse_import_amount(cell("amount"))
            if amount is None:
                warnings.append(f"Row {line_num}: skipped — unparseable amount.")
                continue
            t_type = "expense" if amount < 0 else "income"
            amount = abs(amount)

        rows.append({
            "date": parsed_date.strftime("%Y-%m-%d"),
            "description": description,
            "amount": float(amount),
            "type": t_type,
            "category": _guess_category(description, t_type),
        })

    return rows, warnings


_MONEY = r"-?[$£€৳]?[\d,]+\.\d{2}"
# "9/19" (US-style, resolved against the statement period in
# _resolve_pdf_date), "19-Sep-2024" (day-month-year with no separator space,
# common outside the US — BRAC Bank among others prints exactly this), or
# "19 Sep 2024" / "19 September 2024" (day-month-year with spaces, common on
# UK statements).
_PDF_DATE_PATTERN = (
    r"(?:\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d{1,2}-[A-Za-z]{3}-\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})"
)
# Tried first: some banks (Chase among them) print a running balance right
# after the amount on every line — "... -21.30 15,937.21" — so the
# amount/balance pair has to be matched together, or a naive "last number
# on the line" match grabs the balance instead of the actual amount.
_PDF_TX_LINE_WITH_BALANCE_RE = re.compile(
    rf"^({_PDF_DATE_PATTERN})\s+(.+?)\s+({_MONEY})\s+{_MONEY}$"
)
_PDF_TX_LINE_RE = re.compile(
    rf"^({_PDF_DATE_PATTERN})\s+(.+?)\s+({_MONEY})$"
)
# Separate withdraw/deposit columns plus a running balance — "19-Sep-2024
# CARD ANNUAL FEE **8562 FOR 2024-25 600.00 0.00 4,400.00" — instead of one
# signed amount. Whichever of the two money columns is non-zero is the
# transaction; tried before _PDF_TX_LINE_WITH_BALANCE_RE would otherwise
# misread the deposit column as a second "balance" and drop it.
_PDF_TX_LINE_WITHDRAW_DEPOSIT_RE = re.compile(
    rf"^({_PDF_DATE_PATTERN})\s+(.+?)\s+({_MONEY})\s+({_MONEY})\s+{_MONEY}$"
)

# "June 24, 2026 through July 22, 2026" (pdfplumber often extracts this with
# no whitespace around "through"/"to") — the statement-period line several
# banks (Chase among them) print, used to resolve a bare "MM/DD" transaction
# date (no year at all) that some statements use throughout.
_PDF_STATEMENT_PERIOD_RE = re.compile(
    r"([A-Z][a-z]+)\s+\d{1,2},\s*(\d{4})\s*(?:through|to|-)\s*"
    r"([A-Z][a-z]+)\s+\d{1,2},\s*(\d{4})"
)
_MONTH_NAME_TO_NUM = {name: i for i, name in enumerate(calendar.month_name) if name}


def _resolve_pdf_statement_years(full_text: str) -> tuple[int, int, int, int] | None:
    """(start_month, start_year, end_month, end_year) from a statement-period
    line, or None if the document doesn't have one in a recognized shape."""
    m = _PDF_STATEMENT_PERIOD_RE.search(full_text)
    if not m:
        return None
    start_month_name, start_year, end_month_name, end_year = m.groups()
    start_month = _MONTH_NAME_TO_NUM.get(start_month_name)
    end_month = _MONTH_NAME_TO_NUM.get(end_month_name)
    if start_month is None or end_month is None:
        return None
    return start_month, int(start_year), end_month, int(end_year)


def _resolve_pdf_date(
    date_str: str, year_context: tuple[int, int, int, int] | None, day_first: bool = False
) -> date | None:
    """
    Most banks print a full date on every transaction line, which
    _parse_import_date handles directly. Some (Chase among them) print
    only "MM/DD" with no year anywhere on the line — the year has to come
    from the statement-period text (year_context, from
    _resolve_pdf_statement_years), using the end year unless the
    transaction's month is past the period's end month, which means it
    belongs to the start year (a December/January-spanning statement).
    Falls back to the current year if the document had no resolvable
    period at all, rather than failing every bare-MM/DD date outright.
    """
    parsed = _parse_import_date(date_str, day_first=day_first)
    if parsed is not None:
        return parsed

    try:
        first_str, second_str = date_str.split("/")
        first, second = int(first_str), int(second_str)
    except ValueError:
        return None
    day, month = (first, second) if day_first else (second, first)

    if year_context is not None:
        start_month, start_year, end_month, end_year = year_context
        year = start_year if (start_year != end_year and month > end_month) else end_year
    else:
        year = date.today().year

    try:
        return date(year, month, day)
    except ValueError:
        return None


class PdfPasswordRequired(Exception):
    """Raised by _parse_statement_pdf when the PDF is encrypted and either
    no password or the wrong password was supplied — the route catches
    this specifically so the client can prompt for a password and retry,
    instead of surfacing it as a generic "corrupted file" error."""


def _parse_statement_pdf(file_stream, password: str | None = None) -> tuple[list[dict], list[str]]:
    """
    Parses an uploaded bank-statement PDF by reading each page's plain
    text and matching transaction lines with a regex, rather than
    pdfplumber's table-extraction — real bank statements tested against
    this (not just clean generated fixtures) turned out to almost never
    draw actual table gridlines pdfplumber can detect, and lay text out
    in a way that fragments badly under its "text" table strategy too
    (a header cell like "Description" splitting into "Descr"/"iption").
    A statement line, across every real sample seen, does reliably start
    with a date and end with a dollar amount — "MM/DD[/YY[YY]] ... amount"
    — which is what this matches instead. Continuation lines (an
    "ID:...PPD" trace line under a transaction, wrapped city/state text)
    don't fit that shape and are simply not matched, which is fine — the
    transaction line itself already has a usable description.

    Two format quirks handled explicitly because real statements hit
    them: some banks (Chase) print a running balance right after the
    amount on every line, so the amount+balance pair is tried before the
    amount-only pattern (see _PDF_TX_LINE_WITH_BALANCE_RE); and some print
    the date as bare "MM/DD" with no year anywhere on the line, resolved
    via the statement-period text instead (_resolve_pdf_date).

    Income vs. expense comes from the amount's own sign — negative (or a
    leading "-") is an expense, unsigned is income. An earlier version
    tried to fall back to the nearest "Deposits"/"Withdrawals"-shaped
    section header when the sign was absent, but real statements broke
    that: a summary block near the top of the document (e.g. "Electronic
    Withdrawals -18,910.00") would set the section once, and on a bank
    that never repeats a "Deposits" header inside the actual transaction
    list, every unsigned deposit that followed silently inherited the
    stale "expense" context. Every real statement tested prints a sign
    directly, so it alone is now the source of truth — no section
    tracking to go stale.

    Only works on text-based PDFs (the normal case for a downloaded/
    emailed e-statement); a scanned or photographed page has no
    extractable text layer and isn't supported — flagged clearly rather
    than silently returning nothing.
    """
    # Imported lazily: pdfplumber pulls in Pillow/pypdfium2, real import
    # weight that only this one path needs — no reason to pay it on every
    # app boot for a feature most requests never touch.
    import pdfplumber
    from pdfminer.pdfdocument import PDFPasswordIncorrect
    from pdfplumber.utils.exceptions import PdfminerException

    try:
        pdf = pdfplumber.open(file_stream, password=password or "")
    except PdfminerException as exc:
        if isinstance(exc.args[0] if exc.args else None, PDFPasswordIncorrect):
            raise PdfPasswordRequired() from None
        return [], ["Could not read that PDF — it may be encrypted or corrupted."]
    except Exception:
        return [], ["Could not read that PDF."]

    with pdf:
        pages_text = [page.extract_text() or "" for page in pdf.pages]

    full_text = "\n".join(pages_text)
    any_text_found = any(text.strip() for text in pages_text)
    year_context = _resolve_pdf_statement_years(full_text)
    day_first = _detect_day_first(full_text)

    rows: list[dict] = []
    warnings: list[str] = []

    for text in pages_text:
        for raw_line in text.split("\n"):
            line = raw_line.strip()
            if not line:
                continue

            wd_m = _PDF_TX_LINE_WITHDRAW_DEPOSIT_RE.match(line)
            if wd_m:
                date_str, description, withdraw_str, deposit_str = wd_m.groups()
                parsed_date = _resolve_pdf_date(date_str, year_context, day_first)
                if parsed_date is None:
                    continue

                withdraw = _parse_import_amount(withdraw_str.lstrip("$£€৳"))
                deposit = _parse_import_amount(deposit_str.lstrip("$£€৳"))
                if withdraw is None or deposit is None:
                    continue

                if withdraw != 0:
                    t_type, amount = "expense", abs(withdraw)
                elif deposit != 0:
                    t_type, amount = "income", abs(deposit)
                else:
                    continue

                # A statement whose description wraps onto the line before
                # or after can leave a transaction line that's nothing but
                # date + numbers — the regex still "matches" by taking a
                # leading digit-string as the description (e.g. "500.00"
                # split into desc="5", amount="00.00"). No real merchant
                # name is ever pure digits/punctuation, so reject that
                # rather than import a transaction with a garbage label.
                description = description.strip()
                if not description or not any(ch.isalpha() for ch in description):
                    warnings.append(
                        f"Skipped a transaction on {parsed_date.strftime('%b %d, %Y')}: "
                        "its description wrapped onto another line and couldn't be matched."
                    )
                    continue

                rows.append({
                    "date": parsed_date.strftime("%Y-%m-%d"),
                    "description": description,
                    "amount": float(amount),
                    "type": t_type,
                    "category": _guess_category(description, t_type),
                })
                continue

            m = _PDF_TX_LINE_WITH_BALANCE_RE.match(line) or _PDF_TX_LINE_RE.match(line)
            if m:
                date_str, description, amount_str = m.groups()
                parsed_date = _resolve_pdf_date(date_str, year_context, day_first)
                if parsed_date is None:
                    continue

                amount = _parse_import_amount(amount_str.lstrip("$£€৳"))
                if amount is None:
                    continue

                t_type = "expense" if amount < 0 or amount_str.lstrip("$£€৳").startswith("-") else "income"
                amount = abs(amount)

                description = description.strip()
                if not description or not any(ch.isalpha() for ch in description):
                    warnings.append(
                        f"Skipped a transaction on {parsed_date.strftime('%b %d, %Y')}: "
                        "its description wrapped onto another line and couldn't be matched."
                    )
                    continue

                rows.append({
                    "date": parsed_date.strftime("%Y-%m-%d"),
                    "description": description,
                    "amount": float(amount),
                    "type": t_type,
                    "category": _guess_category(description, t_type),
                })
                continue

    if not any_text_found:
        return [], [
            "Couldn't read any text from that PDF. This works on text-based statements "
            "(the normal kind a bank emails or lets you download) — a scanned or "
            "photographed page has no extractable text and isn't supported."
        ]
    if not rows:
        return [], [
            "Found text in that PDF, but nothing that looked like a transaction line "
            "(a date, description, and amount together). A CSV export, if your bank "
            "offers one, will work more reliably than a PDF."
        ]

    return rows, warnings


def _csv_formula_safe(value: str) -> str:
    """
    Neutralizes CSV/formula injection: a description starting with
    =, +, -, or @ is interpreted as a formula by Excel/Sheets when the
    exported file is opened, not as literal text. description is free
    text the user themselves typed into the amount/description field on
    /finance, so a value like "=1+1" (or something more deliberately
    malicious) would silently execute as a formula on open. Prefixing
    with a single quote is the standard mitigation — spreadsheet apps
    treat it as forcing plain-text and don't display it.
    """
    if value and value[0] in ("=", "+", "-", "@"):
        return "'" + value
    return value


@finance_bp.route("/finance/export")
@login_required
def export_csv():
    """
    Downloads the signed-in user's transactions as CSV.

    Honors the same ?month=YYYY-MM / ?month=all convention as finance_page()
    so an "Export" link on a filtered view exports exactly what's on
    screen, not a surprise full-history dump.
    """
    uid = current_user.id
    month_param = (request.args.get("month") or "").strip()
    all_time = month_param == "all"

    query = Transaction.query.filter_by(user_id=uid)
    if all_time:
        filename_part = "all-time"
    elif month_param:
        try:
            sel_year, sel_month = (int(part) for part in month_param.split("-", 1))
            selected_date = _month_start(sel_year, sel_month)
        except (ValueError, TypeError):
            selected_date = _month_start(date.today().year, date.today().month)
        next_month = (
            _month_start(selected_date.year + 1, 1)
            if selected_date.month == 12
            else _month_start(selected_date.year, selected_date.month + 1)
        )
        query = query.filter(
            Transaction.timestamp >= selected_date,
            Transaction.timestamp < next_month,
        )
        filename_part = selected_date.strftime("%Y-%m")
    else:
        today = date.today()
        selected_date = _month_start(today.year, today.month)
        next_month = (
            _month_start(today.year + 1, 1) if today.month == 12 else _month_start(today.year, today.month + 1)
        )
        query = query.filter(
            Transaction.timestamp >= selected_date,
            Transaction.timestamp < next_month,
        )
        filename_part = selected_date.strftime("%Y-%m")

    rows = query.order_by(Transaction.timestamp.asc(), Transaction.id.asc()).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Date", "Description", "Type", "Amount", "Category", "Recurring"])
    for t in rows:
        writer.writerow([
            t.timestamp.strftime("%Y-%m-%d") if t.timestamp else "",
            _csv_formula_safe(t.description),
            t.type,
            f"{t.amount:.2f}",
            TRANSACTION_CATEGORIES.get(t.category, {}).get("label", "") if t.category else "",
            "yes" if t.is_recurring else "no",
        ])

    response = Response(buffer.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = f"attachment; filename=artha-transactions-{filename_part}.csv"
    return response


@finance_bp.route("/finance/import/preview", methods=["POST"])
@login_required
def import_preview():
    """
    Parses an uploaded statement (CSV or PDF) and returns what WOULD be
    imported — nothing is saved here. The client renders this as an
    editable table (date/description/amount/type/category per row,
    duplicates pre-unchecked); only import_commit() below actually writes
    anything.
    """
    file = request.files.get("statement")
    if file is None or not file.filename:
        return jsonify({"message": "No file uploaded"}), 400

    is_pdf = file.filename.lower().endswith(".pdf") or file.mimetype == "application/pdf"
    if is_pdf:
        password = request.form.get("pdf_password") or None
        try:
            rows, warnings = _parse_statement_pdf(file.stream, password=password)
        except PdfPasswordRequired:
            message = (
                "That PDF needs a password to open."
                if password is None
                else "That password didn't work."
            )
            return jsonify({"message": message, "needs_password": True}), 400
    else:
        rows, warnings = _parse_statement_csv(file.stream)
    if not rows:
        return jsonify({"message": warnings[0] if warnings else "No transactions found in file."}), 400

    # Flag rows that look like they're already in Artha (same date,
    # description, amount, and type) so the preview can leave them
    # unchecked by default — re-uploading a statement whose date range
    # overlaps a previous import shouldn't double every transaction in it.
    existing = {
        (t.timestamp.strftime("%Y-%m-%d"), t.description, f"{t.amount:.2f}", t.type)
        for t in Transaction.query.filter_by(user_id=current_user.id).all()
    }
    for row in rows:
        key = (row["date"], row["description"], f"{row['amount']:.2f}", row["type"])
        row["duplicate"] = key in existing

    return jsonify({
        "rows": rows,
        "warnings": warnings,
        "categories": {key: val["label"] for key, val in TRANSACTION_CATEGORIES.items()},
    })


@finance_bp.route("/finance/import/commit", methods=["POST"])
@login_required
def import_commit():
    """
    Saves the (possibly user-edited) row list produced by import_preview().
    Each row keeps its own statement date exactly as parsed — a March
    statement imported in July is filed under March, never today.
    """
    data = request.get_json(silent=True) or {}
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows:
        return jsonify({"message": "No rows to import"}), 400

    uid = current_user.id
    max_pos = db.session.query(func.max(Transaction.position)).filter_by(user_id=uid).scalar() or 0

    imported = 0
    skipped = 0
    for row in rows:
        if not isinstance(row, dict):
            skipped += 1
            continue

        description = (row.get("description") or "").strip()
        t_type = row.get("type")
        date_str = (row.get("date") or "").strip()

        if not description or t_type not in ("income", "expense"):
            skipped += 1
            continue

        try:
            amount = Decimal(str(row.get("amount", "")))
            if amount < 0:
                raise InvalidOperation
        except InvalidOperation:
            skipped += 1
            continue

        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            skipped += 1
            continue

        category = row.get("category") or None
        if category not in TRANSACTION_CATEGORIES:
            category = None

        max_pos += 1
        db.session.add(Transaction(
            description=description,
            amount=amount,
            type=t_type,
            user_id=uid,
            position=int(max_pos),
            # Reuses the same date_str -> noon-UTC construction every other
            # transaction date in the app goes through — date_str is
            # already confirmed valid above, so this never hits that
            # helper's own "fall back to today" branch.
            timestamp=_resolve_transaction_timestamp(date_str),
            category=category,
            import_source="csv",
        ))
        imported += 1

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        log.error("Error committing CSV import: %s", e, exc_info=True)
        return jsonify({"message": "Database error"}), 500

    return jsonify({
        "message": f"Imported {imported} transaction{'s' if imported != 1 else ''}.",
        "imported": imported,
        "skipped": skipped,
    })


@finance_bp.route("/finance/budget", methods=["POST"])
@login_required
def set_budget():
    """Upsert the signed-in user's single monthly spending cap."""
    raw = (request.form.get("monthly_cap") or "").strip()

    try:
        cap = _validate_amount(raw) if raw else Decimal("0")
    except ValidationError as exc:
        msg = str(exc)
        if is_ajax_request():
            return jsonify({"message": msg}), 400
        flash(msg, "error")
        return redirect(url_for("finance.finance_page"))

    row = Budget.query.filter_by(user_id=current_user.id).first()
    if row is None:
        row = Budget(user_id=current_user.id, monthly_cap=cap)
        db.session.add(row)
    else:
        row.monthly_cap = cap

    try:
        db.session.commit()
        msg = "Budget updated." if cap > 0 else "Budget cleared."
        if is_ajax_request():
            return jsonify({"message": msg}), 200
        flash(msg, "success")
    except Exception as e:
        db.session.rollback()
        log.error("Error saving budget: %s", e, exc_info=True)
        msg = "Error saving budget."
        if is_ajax_request():
            return jsonify({"message": msg}), 500
        flash(msg, "error")

    return redirect(url_for("finance.finance_page"))


@finance_bp.route("/finance/category-budget", methods=["POST"])
@login_required
def set_category_budget():
    """Upsert one category's monthly spending cap. Separate rows per
    category (CategoryBudget), alongside the single overall Budget above,
    not replacing it."""
    category = (request.form.get("category") or "").strip()
    if category not in TRANSACTION_CATEGORIES or category == "income":
        msg = "Choose a valid category to budget."
        if is_ajax_request():
            return jsonify({"message": msg}), 400
        flash(msg, "error")
        return redirect(url_for("finance.finance_page"))

    raw = (request.form.get("monthly_cap") or "").strip()
    try:
        cap = _validate_amount(raw) if raw else Decimal("0")
    except ValidationError as exc:
        msg = str(exc)
        if is_ajax_request():
            return jsonify({"message": msg}), 400
        flash(msg, "error")
        return redirect(url_for("finance.finance_page"))

    if cap <= 0:
        msg = "Enter an amount greater than zero."
        if is_ajax_request():
            return jsonify({"message": msg}), 400
        flash(msg, "error")
        return redirect(url_for("finance.finance_page"))

    row = CategoryBudget.query.filter_by(user_id=current_user.id, category=category).first()
    if row is None:
        row = CategoryBudget(user_id=current_user.id, category=category, monthly_cap=cap)
        db.session.add(row)
    else:
        row.monthly_cap = cap

    try:
        db.session.commit()
        msg = f"{TRANSACTION_CATEGORIES[category]['label']} budget saved."
        if is_ajax_request():
            return jsonify({"message": msg}), 200
        flash(msg, "success")
    except Exception as e:
        db.session.rollback()
        log.error("Error saving category budget: %s", e, exc_info=True)
        msg = "Error saving budget."
        if is_ajax_request():
            return jsonify({"message": msg}), 500
        flash(msg, "error")

    return redirect(url_for("finance.finance_page"))


@finance_bp.route("/finance/category-budget/<category>/delete", methods=["POST"])
@login_required
def delete_category_budget(category):
    row = CategoryBudget.query.filter_by(user_id=current_user.id, category=category).first()
    if row is not None:
        try:
            db.session.delete(row)
            db.session.commit()
            flash("Category budget removed.", "success")
        except Exception as e:
            db.session.rollback()
            log.error("Error deleting category budget: %s", e, exc_info=True)
            flash("Error removing budget.", "error")

    return redirect(url_for("finance.finance_page"))
