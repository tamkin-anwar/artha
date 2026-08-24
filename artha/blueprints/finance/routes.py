import calendar
import csv
import io
import math
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
from ...utils import is_ajax_request, current_month_bounds, budget_status
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
    "income":        {"label": "Income",             "icon": "trending-up"},
    "housing":       {"label": "Housing",             "icon": "home"},
    "utilities":     {"label": "Utilities",           "icon": "zap"},
    "groceries":     {"label": "Groceries",           "icon": "shopping-cart"},
    "dining":        {"label": "Dining",              "icon": "utensils"},
    "transport":     {"label": "Transport",           "icon": "car"},
    "subscriptions": {"label": "Subscriptions",       "icon": "repeat"},
    "shopping":      {"label": "Shopping",            "icon": "shopping-bag"},
    "health":        {"label": "Health",              "icon": "heart-pulse"},
    "entertainment": {"label": "Entertainment",       "icon": "clapperboard"},
    "debt":          {"label": "Debt & loans",        "icon": "credit-card"},
    "other":         {"label": "Other",               "icon": "more-horizontal"},
}

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
        # _next_due_date() in artha/blueprints/dashboard/routes.py).
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

    # Distinct recurring "rules" (by description + type), not a raw row
    # count — each rule accumulates one generated row per month, so a raw
    # count would grow every month even though nothing new was configured.
    recurring_rows = Transaction.query.filter_by(user_id=uid, is_recurring=True).all()
    recurring_count = len({(t.description, t.type) for t in recurring_rows})

    # Always the real current month's spend, independent of whatever month
    # is being browsed above — "my budget" means this calendar month, not
    # whichever one the filter tabs happen to be showing.
    budget_row = Budget.query.filter_by(user_id=uid).first()
    current_month_expense = bucket_for(_month_start(today.year, today.month))["expense"]
    budget = budget_status(budget_row.monthly_cap if budget_row else None, current_month_expense)

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
        budget=budget,
        budget_cap_raw=(budget_row.monthly_cap if budget_row else None),
        today_date=today.strftime("%Y-%m-%d"),
        categories=TRANSACTION_CATEGORIES,
    )


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
    "%m/%d/%y",
    "%d/%m/%y",
]

_DATE_HEADER_ALIASES = {"date", "transaction date", "posted date", "trans date", "post date", "value date"}
_DESC_HEADER_ALIASES = {"description", "memo", "narrative", "details", "payee", "merchant", "particulars"}
_AMOUNT_HEADER_ALIASES = {"amount", "transaction amount", "amt"}
_DEBIT_HEADER_ALIASES = {"debit", "withdrawal", "withdrawals", "money out", "dr"}
_CREDIT_HEADER_ALIASES = {"credit", "deposit", "deposits", "money in", "cr"}


def _parse_import_date(raw: str) -> date | None:
    """
    Tries each statement date format banks commonly export in, in order.
    Ambiguous day/month ordering (e.g. "03/04") is resolved US-first —
    the preview step (every parsed date is shown before anything is
    saved) is what catches a wrong guess, not this function. Critically,
    this NEVER falls back to today: a row whose date can't be parsed is
    skipped (see _parse_statement_csv), not silently dated to the day of
    the upload — a March statement imported in July must file under
    March, or the whole point of importing history is defeated.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in _IMPORT_DATE_FORMATS:
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
    """Strips common statement formatting ($, commas, parens-for-negative)
    before parsing — a bank export rarely hands back a bare number."""
    raw = (raw or "").strip()
    if not raw:
        return None
    negative = raw.startswith("(") and raw.endswith(")")
    cleaned = raw.strip("()").replace("$", "").replace(",", "").strip()
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
    warnings: list[str] = []
    try:
        text = file_stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            file_stream.seek(0)
            text = file_stream.read().decode("latin-1")
        except Exception:
            return [], ["Could not read file — please export as CSV (UTF-8 or Latin-1)."]

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

    rows: list[dict] = []
    for line_num, raw_row in enumerate(reader, start=2):
        if not raw_row or all(not cell.strip() for cell in raw_row):
            continue

        def cell(role: str) -> str:
            idx = roles.get(role)
            return raw_row[idx].strip() if idx is not None and idx < len(raw_row) else ""

        parsed_date = _parse_import_date(cell("date"))
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
    Parses an uploaded statement CSV and returns what WOULD be imported —
    nothing is saved here. The client renders this as an editable table
    (date/description/amount/type/category per row, duplicates pre-
    unchecked); only import_commit() below actually writes anything.
    """
    file = request.files.get("statement")
    if file is None or not file.filename:
        return jsonify({"message": "No file uploaded"}), 400

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
        flash(str(exc), "error")
        return redirect(url_for("finance.finance_page"))

    row = Budget.query.filter_by(user_id=current_user.id).first()
    if row is None:
        row = Budget(user_id=current_user.id, monthly_cap=cap)
        db.session.add(row)
    else:
        row.monthly_cap = cap

    try:
        db.session.commit()
        flash("Budget updated." if cap > 0 else "Budget cleared.", "success")
    except Exception as e:
        db.session.rollback()
        log.error("Error saving budget: %s", e, exc_info=True)
        flash("Error saving budget.", "error")

    return redirect(url_for("finance.finance_page"))
