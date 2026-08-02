import calendar as cal
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from flask import render_template, redirect, url_for, request, flash, jsonify
from flask_login import login_required, current_user
from sqlalchemy import func

from ...extensions import db
from ...models import Note, Transaction, Event
from ...services.exchange_rate_service import get_rates
from ...utils import current_month_bounds, derive_title_and_preview
from . import dashboard_bp

log = logging.getLogger(__name__)

# Same closed 6-color set as Notes' NOTE_COLORS (artha/blueprints/notes/routes.py)
# — reused rather than importing across blueprints for one small constant.
EVENT_COLORS = {"sage", "coral", "plum", "slate", "sky", "amber"}


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
            derived_title, preview = derive_title_and_preview(note_content)
            new_note = Note(
                title=derived_title,
                content=note_content,
                preview=preview,
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

    uid = current_user.id
    today = date.today()

    notes = (
        Note.query.filter_by(user_id=uid)
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
            Transaction.user_id == uid,
            Transaction.timestamp >= month_start,
            Transaction.timestamp < month_end,
        )
        .order_by(Transaction.position.asc(), Transaction.id.asc())
        .all()
    )
    income = (
        db.session.query(func.sum(Transaction.amount))
        .filter(
            Transaction.user_id == uid,
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
            Transaction.user_id == uid,
            Transaction.type == "expense",
            Transaction.timestamp >= month_start,
            Transaction.timestamp < month_end,
        )
        .scalar()
        or 0
    )
    income = float(income)
    expense = float(expense)
    balance = income - expense

    # ------------------------------------------------------------------
    # "Today" panel — time-blocked events + notes due today/overdue.
    # Calendar and Notes both track time-sensitive things that never
    # otherwise surface outside their own pages.
    # ------------------------------------------------------------------
    today_start_dt = datetime(today.year, today.month, today.day)
    today_end_dt = today_start_dt + timedelta(days=1)

    def _fmt_time(dt: datetime) -> str:
        hour12 = dt.hour % 12 or 12
        return f"{hour12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"

    todays_events = [
        {
            "id": e.id,
            "title": e.title,
            "color": e.color,
            "start_label": _fmt_time(e.start),
            "end_label": _fmt_time(e.end),
        }
        for e in Event.query.filter(
            Event.user_id == uid,
            Event.start >= today_start_dt,
            Event.start < today_end_dt,
        )
        .order_by(Event.start.asc())
        .all()
    ]

    notes_due_or_overdue = (
        Note.query.filter(
            Note.user_id == uid,
            Note.due_date.isnot(None),
            Note.due_date <= today,
        )
        .order_by(Note.due_date.asc(), Note.pinned.desc())
        .all()
    )
    overdue_notes = [n for n in notes_due_or_overdue if n.due_date < today]
    due_today_notes = [n for n in notes_due_or_overdue if n.due_date == today]

    # ------------------------------------------------------------------
    # Recurring renewals landing within the next 7 days — same
    # dedup-by-(description,type)-then-_next_due_date() approach as the
    # calendar page's "upcoming recurring" banner, just widened from one
    # nearest hit to the whole week for a dashboard callout.
    # ------------------------------------------------------------------
    recurring_rows = Transaction.query.filter_by(user_id=uid, is_recurring=True).all()
    templates_by_key: dict[tuple[str, str], Transaction] = {}
    for t in recurring_rows:
        key = (t.description, t.type)
        current = templates_by_key.get(key)
        if current is None or (t.timestamp and current.timestamp and t.timestamp > current.timestamp):
            templates_by_key[key] = t

    renewals_this_week = []
    for (desc, ttype), tx in templates_by_key.items():
        due = _next_due_date(tx, today)
        if due is not None and 0 <= (due - today).days <= 7:
            renewals_this_week.append({
                "description": desc,
                "type": ttype,
                "amount": float(tx.amount),
                "due": due,
                "due_label": "Today" if due == today else f"{cal.month_abbr[due.month]} {due.day}",
            })
    renewals_this_week.sort(key=lambda e: e["due"])
    renewals_total = sum(e["amount"] for e in renewals_this_week if e["type"] == "expense")

    # ------------------------------------------------------------------
    # One-line contextual summary, capped at 3 clauses so it stays a
    # single readable line even on a busy day.
    # ------------------------------------------------------------------
    summary_parts = []
    if todays_events:
        n = len(todays_events)
        summary_parts.append(f"{n} event{'s' if n != 1 else ''} today")
    if overdue_notes:
        n = len(overdue_notes)
        summary_parts.append(f"{n} note{'s' if n != 1 else ''} overdue")
    elif due_today_notes:
        n = len(due_today_notes)
        summary_parts.append(f"{n} note{'s' if n != 1 else ''} due today")
    if renewals_this_week:
        summary_parts.append(f"${renewals_total:,.0f} in renewals this week")
    summary_parts.append("spending on pace" if balance >= 0 else "spending ahead of income this month")
    dashboard_summary = " · ".join(summary_parts[:3])

    return render_template(
        "index.html",
        notes=notes,
        transactions=transactions,
        income=income,
        expense=expense,
        balance=balance,
        today=today,
        today_date=today.strftime("%Y-%m-%d"),
        todays_events=todays_events,
        overdue_notes=overdue_notes,
        due_today_notes=due_today_notes,
        renewals_this_week=renewals_this_week,
        renewals_total=renewals_total,
        dashboard_summary=dashboard_summary,
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

    # Always computed from the real today (not the viewed month) — the due
    # date itself can land in the next calendar month (e.g. today is Jul 31,
    # due Aug 1), so gating this to "only while viewing today's month" would
    # hide it on the one day it's actually about. The template renders this
    # unconditionally; script.js decides *when* to surface it (today's cell
    # or the due date's own cell), so being in the DOM here doesn't mean
    # it's visible on every day.
    upcoming_recurring = None
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

    # Time-blocked events within the visible window — grouped by the date
    # of their start time, same as Transaction's by_date above (an event
    # spanning midnight isn't split across two days, matches how a
    # Transaction dot works too).
    events = (
        Event.query.filter(
            Event.user_id == uid,
            Event.start >= fetch_start_dt,
            Event.start < fetch_end_dt,
        )
        .order_by(Event.start.asc())
        .all()
    )
    events_by_date = defaultdict(list)
    for e in events:
        events_by_date[e.start.strftime("%Y-%m-%d")].append(e)

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
            "event_dot": key in events_by_date,
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

    calendar_events = {
        key: [
            {
                "id": e.id,
                "title": e.title,
                "start": e.start.isoformat(),
                "end": e.end.isoformat(),
                "color": e.color,
            }
            for e in day_events
        ]
        for key, day_events in events_by_date.items()
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
        calendar_events=calendar_events,
        recurring_due_by_date=dict(recurring_due_by_date),
        upcoming_recurring=upcoming_recurring,
    )


# ---------------------------------------------------------------------------
# Calendar events — time-blocked entries (JSON CRUD backing the day panel's
# drag-to-schedule grid). Same conventions as artha/blueprints/notes/routes.py:
# PATCH for field updates, POST /.../delete for deletes (no DELETE verb used
# anywhere else in this app).
# ---------------------------------------------------------------------------

def _serialize_event(event: Event) -> dict:
    return {
        "id": event.id,
        "title": event.title,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "color": event.color,
    }


def _parse_local_datetime(raw):
    """Parse a naive local "YYYY-MM-DDTHH:MM:SS"-style string from the
    client. This app stores every other timestamp (Transaction.timestamp,
    Note.due_date) as naive local time with no timezone conversion — Event
    follows the same convention, so any tzinfo the client did send (e.g. a
    trailing "Z") is stripped rather than honored, keeping "10am" meaning
    the same 10am everywhere else in the app."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


@dashboard_bp.route("/calendar/events", methods=["POST"])
@login_required
def create_event():
    data = request.get_json(silent=True) or {}

    title = (data.get("title") or "").strip()
    start = _parse_local_datetime(data.get("start"))
    end = _parse_local_datetime(data.get("end"))
    color = data.get("color")

    if not title:
        return jsonify({"message": "Title is required"}), 400
    if start is None or end is None:
        return jsonify({"message": "Invalid start/end time"}), 400
    if end <= start:
        return jsonify({"message": "End must be after start"}), 400
    if color not in EVENT_COLORS:
        color = "sky"

    event = Event(user_id=current_user.id, title=title, start=start, end=end, color=color)
    try:
        db.session.add(event)
        db.session.commit()
        return jsonify(_serialize_event(event)), 201
    except Exception as e:
        db.session.rollback()
        log.error("Error creating event: %s", e, exc_info=True)
        return jsonify({"message": "Database error"}), 500


@dashboard_bp.route("/calendar/events/<int:event_id>", methods=["PATCH"])
@login_required
def update_event(event_id):
    event = db.session.get(Event, event_id)
    if event is None:
        return jsonify({"message": "Not found"}), 404
    if event.user_id != current_user.id:
        return jsonify({"message": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}

    new_start = event.start
    new_end = event.end
    if "start" in data:
        parsed = _parse_local_datetime(data.get("start"))
        if parsed is None:
            return jsonify({"message": "Invalid start time"}), 400
        new_start = parsed
    if "end" in data:
        parsed = _parse_local_datetime(data.get("end"))
        if parsed is None:
            return jsonify({"message": "Invalid end time"}), 400
        new_end = parsed
    if new_end <= new_start:
        return jsonify({"message": "End must be after start"}), 400
    event.start = new_start
    event.end = new_end

    if "title" in data:
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"message": "Title is required"}), 400
        event.title = title

    if "color" in data:
        color = data.get("color")
        if color in EVENT_COLORS:
            event.color = color

    try:
        db.session.commit()
        return jsonify(_serialize_event(event))
    except Exception as e:
        db.session.rollback()
        log.error("Error updating event: %s", e, exc_info=True)
        return jsonify({"message": "Database error"}), 500


@dashboard_bp.route("/calendar/events/<int:event_id>/delete", methods=["POST"])
@login_required
def delete_event(event_id):
    event = db.session.get(Event, event_id)
    if event is None:
        return jsonify({"message": "Not found"}), 404
    if event.user_id != current_user.id:
        return jsonify({"message": "Unauthorized"}), 403

    try:
        db.session.delete(event)
        db.session.commit()
        return jsonify({"message": "Event deleted"})
    except Exception as e:
        db.session.rollback()
        log.error("Error deleting event: %s", e, exc_info=True)
        return jsonify({"message": "Database error"}), 500


# ---------------------------------------------------------------------------
# Calculator — full page (Numi-style smart expression editor + button pad)
# ---------------------------------------------------------------------------

@dashboard_bp.route("/calculator")
@login_required
def calculator_page():
    # Entirely client-side (math.js does the evaluation in the browser) —
    # no server-side data needed for the page itself. Currency conversion
    # is the one exception (below) — live rates can't live in the browser.
    return render_template("calculator.html")


@dashboard_bp.get("/calculator/exchange_rates")
@login_required
def calculator_exchange_rates():
    rates = get_rates()
    if rates is None:
        return jsonify({"error": "unavailable"}), 503
    return jsonify(rates)
