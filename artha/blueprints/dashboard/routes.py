import calendar as cal
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from flask import render_template, redirect, url_for, request, flash, jsonify
from flask_login import login_required, current_user
from sqlalchemy import func

from ...extensions import db
from ...models import Note, Transaction
from ...utils import current_month_bounds
from . import dashboard_bp

log = logging.getLogger(__name__)


@dashboard_bp.get("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200


@dashboard_bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        note_content = request.form.get("note", "").strip()
        if note_content:
            max_pos = (
                db.session.query(func.max(Note.position))
                .filter_by(user_id=current_user.id)
                .scalar()
                or 0
            )
            new_note = Note(
                content=note_content,
                user_id=current_user.id,
                position=int(max_pos) + 1,
            )
            try:
                db.session.add(new_note)
                db.session.commit()
                flash("Note added!", "success")
            except Exception as e:
                db.session.rollback()
                log.error("Error adding note: %s", e, exc_info=True)
                flash("Error adding note", "error")
        return redirect(url_for("dashboard.index"))

    notes = (
        Note.query.filter_by(user_id=current_user.id)
        .order_by(Note.position.asc(), Note.id.asc())
        .all()
    )
    # Dashboard cards are scoped to the current calendar month — same
    # default the /finance page's month tabs use. This used to sum every
    # transaction the user ever entered, all-time, which made the cards
    # both misleading and inconsistent with the rest of the app.
    month_start, month_end = current_month_bounds()

    transactions = (
        Transaction.query.filter(
            Transaction.user_id == current_user.id,
            Transaction.timestamp >= month_start,
            Transaction.timestamp < month_end,
        )
        .order_by(Transaction.position.asc(), Transaction.id.asc())
        .all()
    )
    income = (
        db.session.query(func.sum(Transaction.amount))
        .filter(
            Transaction.user_id == current_user.id,
            Transaction.type == "income",
            Transaction.timestamp >= month_start,
            Transaction.timestamp < month_end,
        )
        .scalar()
        or 0
    )
    expense = (
        db.session.query(func.sum(Transaction.amount))
        .filter(
            Transaction.user_id == current_user.id,
            Transaction.type == "expense",
            Transaction.timestamp >= month_start,
            Transaction.timestamp < month_end,
        )
        .scalar()
        or 0
    )

    return render_template(
        "index.html",
        notes=notes,
        transactions=transactions,
        income=float(income),
        expense=float(expense),
        balance=float(income - expense),
    )


# ---------------------------------------------------------------------------
# Calendar — full page (Fantastical-style month grid + day detail panel)
# ---------------------------------------------------------------------------

def _next_due_date(template_tx: Transaction, from_date: date) -> date | None:
    """
    This app has no explicit "day of month" field for recurring rules —
    a recurring transaction is just a row with is_recurring=True that gets
    a fresh copy generated on whatever date the user next loads /finance
    (see generate_recurring() in finance/routes.py). So the day-of-month
    of the most recent occurrence is the best available signal for when
    it "usually" lands. Clamped to the last day of shorter months (e.g.
    day 31 in February -> the 28th/29th).
    """
    day_of_month = template_tx.timestamp.day
    year, month = from_date.year, from_date.month
    for _ in range(13):  # defensive cap: at most one year of scanning
        days_this_month = cal.monthrange(year, month)[1]
        candidate = date(year, month, min(day_of_month, days_this_month))
        if candidate >= from_date:
            return candidate
        month += 1
        if month == 13:
            month = 1
            year += 1
    return None


@dashboard_bp.route("/calendar")
@login_required
def calendar_page():
    uid = current_user.id
    today = date.today()

    month_param = (request.args.get("month") or "").strip()
    month_was_explicit = bool(month_param)
    if month_param:
        try:
            year, month = (int(part) for part in month_param.split("-", 1))
        except (ValueError, TypeError):
            year, month = today.year, today.month
    else:
        year, month = today.year, today.month

    first_of_month = date(year, month, 1)
    days_in_month = cal.monthrange(year, month)[1]
    last_of_month = date(year, month, days_in_month)

    # Sunday-first grid. date.weekday(): Monday=0..Sunday=6, so shift by 1
    # to get a Sunday=0..Saturday=6 index for padding math.
    leading = (first_of_month.weekday() + 1) % 7
    grid_start = first_of_month - timedelta(days=leading)
    trailing = 6 - ((last_of_month.weekday() + 1) % 7)
    grid_end = last_of_month + timedelta(days=trailing)

    # Padded another 7 days each side per spec, so transactions right at
    # the visible grid's edges are always available for the dots even if
    # the grid math above is ever off by a day in some locale/edge case.
    fetch_start = grid_start - timedelta(days=7)
    fetch_end = grid_end + timedelta(days=7)
    fetch_start_dt = datetime(fetch_start.year, fetch_start.month, fetch_start.day)
    fetch_end_dt = datetime(fetch_end.year, fetch_end.month, fetch_end.day) + timedelta(days=1)

    txs = (
        Transaction.query.filter(
            Transaction.user_id == uid,
            Transaction.timestamp >= fetch_start_dt,
            Transaction.timestamp < fetch_end_dt,
        )
        .order_by(Transaction.timestamp.asc())
        .all()
    )

    by_date = defaultdict(list)
    for t in txs:
        by_date[t.timestamp.strftime("%Y-%m-%d")].append(t)

    # Recurring rules: most-recent row per (description, type) — same
    # dedup pattern as generate_recurring() in finance/routes.py, since
    # each recurring rule accumulates one row per month it's been active.
    recurring_rows = Transaction.query.filter_by(user_id=uid, is_recurring=True).all()
    templates_by_key: dict[tuple[str, str], Transaction] = {}
    for t in recurring_rows:
        key = (t.description, t.type)
        current = templates_by_key.get(key)
        if current is None or (t.timestamp and current.timestamp and t.timestamp > current.timestamp):
            templates_by_key[key] = t

    recurring_due_by_date = defaultdict(list)
    all_due = []
    for (desc, ttype), tx in templates_by_key.items():
        due = _next_due_date(tx, today)
        if due is None:
            continue
        entry = {
            "date": due.strftime("%Y-%m-%d"),
            "date_label": f"{cal.month_abbr[due.month]} {due.day}",
            "description": desc,
            "amount": float(tx.amount),
            "type": ttype,
        }
        recurring_due_by_date[entry["date"]].append(entry)
        all_due.append(entry)

    # This is a "heads up" nudge anchored to the real today, not to
    # whatever day/month is currently being browsed — so it's only shown
    # while viewing the month that today actually falls in. Otherwise it
    # sat, unchanged, under every month you navigated to, looking like it
    # belonged to whatever was on screen when it didn't.
    upcoming_recurring = None
    if year == today.year and month == today.month:
        within_7 = [e for e in all_due if 0 <= (datetime.strptime(e["date"], "%Y-%m-%d").date() - today).days <= 7]
        if within_7:
            within_7.sort(key=lambda e: e["date"])
            upcoming_recurring = within_7[0]

    # Notes due within the visible window — due_date is a plain Date
    # column, so it compares directly against fetch_start/fetch_end
    # (the date objects, not the _dt datetimes built for Transaction).
    notes_due = (
        Note.query.filter(
            Note.user_id == uid,
            Note.due_date.isnot(None),
            Note.due_date >= fetch_start,
            Note.due_date <= fetch_end,
        )
        .order_by(Note.pinned.desc(), Note.due_date.asc(), Note.id.asc())
        .all()
    )
    notes_by_date = defaultdict(list)
    for n in notes_due:
        notes_by_date[n.due_date.strftime("%Y-%m-%d")].append(n)

    grid_days = []
    cursor = grid_start
    while cursor <= grid_end:
        key = cursor.strftime("%Y-%m-%d")
        day_txs = by_date.get(key, [])
        net = sum((t.amount if t.type == "income" else -t.amount for t in day_txs), Decimal("0"))

        grid_days.append({
            "date": key,
            "day": cursor.day,
            "in_month": cursor.month == month,
            "is_today": cursor == today,
            "is_weekend": cursor.weekday() in (5, 6),
            "income_dot": any(t.type == "income" for t in day_txs),
            "expense_dot": any(t.type == "expense" for t in day_txs),
            "recurring_dot": key in recurring_due_by_date,
            "note_dot": key in notes_by_date,
            "net": float(net),
        })
        cursor += timedelta(days=1)

    # JSON payload for the right panel — the whole point is that clicking
    # a day is instant with no fetch, so every visible day's transactions
    # (including padding overflow into prev/next month) are embedded here.
    calendar_data = {
        key: [
            {
                "id": t.id,
                "description": t.description,
                "amount": float(t.amount),
                "type": t.type,
                "is_recurring": t.is_recurring,
            }
            for t in day_txs
        ]
        for key, day_txs in by_date.items()
    }

    # Kept as a separate parallel blob (not merged into calendar_data's
    # shape) — same pattern already used for recurring_due_by_date
    # alongside calendar_data, so renderDayDetail()'s existing
    # transaction-rendering logic stays untouched.
    calendar_notes = {
        key: [
            {
                "id": n.id,
                "title": n.title or "Untitled",
                "tag": n.tag,
                "color": n.color,
                "pinned": n.pinned,
            }
            for n in day_notes
        ]
        for key, day_notes in notes_by_date.items()
    }

    month_label = f"{cal.month_name[month]} {year}"
    prev_month_value = f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"
    next_month_value = f"{year + 1}-01" if month == 12 else f"{year}-{month + 1:02d}"
    current_month_value = f"{year}-{month:02d}"

    return render_template(
        "calendar.html",
        grid_days=grid_days,
        month_label=month_label,
        prev_month_value=prev_month_value,
        next_month_value=next_month_value,
        current_month_value=current_month_value,
        month_was_explicit=month_was_explicit,
        today_value=today.strftime("%Y-%m-%d"),
        calendar_data=calendar_data,
        calendar_notes=calendar_notes,
        recurring_due_by_date=dict(recurring_due_by_date),
        upcoming_recurring=upcoming_recurring,
    )


# ---------------------------------------------------------------------------
# Calculator — full page (Numi-style smart expression editor + button pad)
# ---------------------------------------------------------------------------

@dashboard_bp.route("/calculator")
@login_required
def calculator_page():
    # Entirely client-side (math.js does the evaluation in the browser) —
    # no server-side data needed.
    return render_template("calculator.html")
