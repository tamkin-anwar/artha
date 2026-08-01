import calendar
import time
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from flask import render_template, redirect, url_for, request, flash, session, jsonify
from flask_login import login_required, current_user
from sqlalchemy import func

from ...extensions import db
from ...models import Transaction
from ...utils import is_ajax_request, current_month_bounds
from . import finance_bp

log = logging.getLogger(__name__)


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
        parsed = datetime.now(timezone.utc).date()
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
    )

    try:
        db.session.add(new_tx)
        db.session.commit()
        if is_ajax_request():
            return render_template(
                "partials/transaction_row.html",
                tx=new_tx,
                today_date=date.today().strftime("%Y-%m-%d"),
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


@finance_bp.route("/reorder_transactions", methods=["POST"])
@login_required
def reorder_transactions():
    data = request.get_json(silent=True) or {}
    order = data.get("order")

    if not isinstance(order, list) or not order:
        return jsonify({"message": "Invalid order payload."}), 400

    try:
        ids = [int(x) for x in order]
    except Exception:
        return jsonify({"message": "Order must be a list of integers."}), 400

    txs = Transaction.query.filter(
        Transaction.user_id == current_user.id,
        Transaction.id.in_(ids),
    ).all()

    if {t.id for t in txs} != set(ids):
        return jsonify({"message": "Order contains unknown or unauthorized transaction ids."}), 403

    id_to_tx = {t.id: t for t in txs}
    for idx, tx_id in enumerate(ids, start=1):
        id_to_tx[tx_id].position = idx

    try:
        db.session.commit()
        return jsonify({"message": "Transaction order saved."})
    except Exception as e:
        db.session.rollback()
        log.error("Error saving transaction order: %s", e, exc_info=True)
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
        "timestamp": (
            tx.timestamp.replace(tzinfo=timezone.utc).isoformat()
            if tx.timestamp
            else datetime.now(timezone.utc).isoformat()
        ),
        "deleted_at": time.time(),
    }

    try:
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
        )
        db.session.add(restored)
        db.session.commit()
        session.pop("last_deleted_tx", None)

        row_html = render_template(
            "partials/transaction_row.html",
            tx=restored,
            today_date=date.today().strftime("%Y-%m-%d"),
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

    tx.is_recurring = not tx.is_recurring

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
        )
        db.session.add(new_tx)
        generated += 1

    if generated:
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

    # Biggest expense "category" (first word of the description, per spec)
    # and the single day of the month with the most spending.
    expense_txs = [t for t in transactions if t.type == "expense"] if not all_time else [
        t for t in all_tx if t.type == "expense"
    ]

    category_totals: dict[str, Decimal] = defaultdict(Decimal)
    day_totals: dict[int, Decimal] = defaultdict(Decimal)
    for t in expense_txs:
        first_word = (t.description or "").strip().split(" ")[0] if (t.description or "").strip() else "Other"
        category_totals[first_word.capitalize()] += t.amount
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
        biggest_category=biggest_category,
        biggest_day_label=biggest_day_label,
        trend_data=trend_data,
        recurring_count=recurring_count,
        today_date=today.strftime("%Y-%m-%d"),
    )
