import calendar as cal
import logging
import secrets
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from flask import current_app, render_template, redirect, url_for, request, flash, jsonify, Response, abort
from flask_login import login_required, current_user
from icalendar import Alarm, Calendar as ICalendar, Event as ICalEvent
from sqlalchemy import func

from ...changelog import CHANGELOG_ENTRIES
from ...extensions import db, ai_limit, AI_CALCULATOR_LIMIT
from ...models import Note, Transaction, Event, EventException, User
from ...services.exchange_rate_service import get_rates, convert_usd_to
from ...services.screens import today_screen
from ...utils import derive_title_and_preview, sanitize_note_html, next_due_date, user_today, is_ajax_request, CURRENCY_SYMBOLS
from ..finance.routes import TRANSACTION_CATEGORIES, generate_due_recurring
from . import dashboard_bp

log = logging.getLogger(__name__)

# Same closed 6-color set as Notes' NOTE_COLORS (artha/blueprints/notes/routes.py)
# — reused rather than importing across blueprints for one small constant.
EVENT_COLORS = {"sage", "coral", "plum", "slate", "sky", "amber"}
EVENT_RECURRENCES = {"daily", "weekly", "monthly"}


@dashboard_bp.get("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200


@dashboard_bp.route("/", methods=["GET", "POST"])
def index():
    # Signed out, the home page introduces Artha instead of jumping to the
    # login form: it's the public homepage Google's and Apple's reviews see.
    if not current_user.is_authenticated:
        if request.method == "GET" and not is_ajax_request():
            return render_template("welcome.html")
        return current_app.login_manager.unauthorized()
    if request.method == "POST":
        note_content = sanitize_note_html(request.form.get("note", "").strip())
        if not note_content:
            msg = "Note content is required."
            if is_ajax_request():
                return jsonify({"message": msg}), 400
            flash(msg, "error")
            return redirect(url_for("dashboard.index"))

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
            if is_ajax_request():
                return jsonify({"message": "Note added!"})
            flash("Note added!", "success")
        except Exception as e:
            db.session.rollback()
            log.error("Error adding note: %s", e, exc_info=True)
            msg = "Error adding note"
            if is_ajax_request():
                return jsonify({"message": msg}), 500
            flash(msg, "error")
        return redirect(url_for("dashboard.index"))

    uid = current_user.id
    # The user's own local date, not the server's — otherwise the "Today"
    # panel and the month's figures can disagree with the viewer's own
    # calendar near a timezone boundary (User.timezone, auto-detected, see
    # static/js/settings.js).
    today = user_today(current_user)

    # Recurring bills used to be created only when /finance was opened, so
    # the Dashboard's own totals and Safe to Spend quietly missed any bill
    # that had come due since. Same catch-up Finance runs (and the same
    # "materialize on GET" precedent _generate_recurring_events sets for
    # Calendar); a failure here must never take the Dashboard down with it.
    try:
        generate_due_recurring(current_user)
    except Exception:
        db.session.rollback()

    # The money on Today is the native app's Today screen, the same dict
    # its API returns (services/screens.py), so the website and the
    # iPhone app can never show different numbers.
    screen = today_screen(current_user, today)

    # ------------------------------------------------------------------
    # "Also today": events and notes due, for the people who use Notes
    # and Calendar here (the redesigned Today is about money; this keeps
    # what's due today from disappearing for them). Only shown when
    # there's something to show.
    # ------------------------------------------------------------------
    today_start_dt = datetime(today.year, today.month, today.day)
    today_end_dt = today_start_dt + timedelta(days=1)

    # A recurring event series only gets real Event rows for whatever
    # window someone actually queries (see _generate_recurring_events's
    # own docstring) — normally that's /calendar loading. Without this,
    # today's occurrence of a recurring event silently wouldn't show up
    # here if nobody's opened Calendar recently enough to have already
    # materialized it.
    _generate_recurring_events(uid, today_start_dt, today_end_dt)

    def _fmt_time(dt: datetime) -> str:
        hour12 = dt.hour % 12 or 12
        return f"{hour12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"

    todays_events = [
        {"id": e.id, "title": e.title, "color": e.color,
         "start_label": _fmt_time(e.start), "end_label": _fmt_time(e.end)}
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
            Note.archived.is_(False),
        )
        .order_by(Note.due_date.asc(), Note.pinned.desc())
        .all()
    )
    overdue_notes = [n for n in notes_due_or_overdue if n.due_date < today]
    due_today_notes = [n for n in notes_due_or_overdue if n.due_date == today]

    for bill in screen["coming_up"]:
        due = date.fromisoformat(bill["due"])
        bill["weekday"] = due.strftime("%a").upper()
        bill["month_label"] = due.strftime("%b").upper()
        bill["day"] = f"{due.day:02d}"
    for tx in screen["recent"]:
        tx_date = date.fromisoformat(tx["date"]) if tx["date"] else today
        tx["when"] = _relative_day(tx_date, today)

    return render_template(
        "index.html",
        screen=screen,
        today=today,
        todays_events=todays_events,
        overdue_notes=overdue_notes,
        due_today_notes=due_today_notes,
        categories=TRANSACTION_CATEGORIES,
    )


def _relative_day(day: date, today: date) -> str:
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    if 0 < (today - day).days < 7:
        return day.strftime("%A")
    return day.strftime("%b %-d")


# ---------------------------------------------------------------------------
# Calendar — full page (Fantastical-style month grid + day detail panel)
# ---------------------------------------------------------------------------


def _advance_recurrence(dt: datetime, cadence: str) -> datetime:
    """Next occurrence of `dt` under the given cadence, preserving time of
    day. Monthly clamps to the shorter month's last day, same clamping
    utils.next_due_date() uses for recurring transactions."""
    if cadence == "daily":
        return dt + timedelta(days=1)
    if cadence == "weekly":
        return dt + timedelta(weeks=1)
    if cadence == "monthly":
        year, month = dt.year, dt.month + 1
        if month == 13:
            month = 1
            year += 1
        days_this_month = cal.monthrange(year, month)[1]
        return dt.replace(year=year, month=month, day=min(dt.day, days_this_month))
    raise ValueError(f"Unknown recurrence cadence: {cadence!r}")


def _generate_recurring_events(uid: int, window_start: datetime, window_end: datetime) -> None:
    """
    Lazily materializes real Event rows for every recurring series whose
    occurrences land inside [window_start, window_end) — the calendar's
    padded fetch window for whatever month is being viewed. Mirrors
    generate_recurring() in finance/routes.py: real rows generated on page
    load rather than a virtual RRULE expansion, so every occurrence stays a
    fully normal, independently editable/deletable Event and nothing else
    in the drag/edit/delete code needs to know recurrence exists.

    Capped at MAX_GENERATED per call as a defensive guard against a
    pathological cadence/window combination — a single month's padded
    window never comes close to this in normal use.
    """
    MAX_GENERATED = 60

    anchors = Event.query.filter(
        Event.user_id == uid,
        Event.recurrence.isnot(None),
        Event.recurrence_parent_id.is_(None),
    ).all()
    if not anchors:
        return

    generated = 0
    for anchor in anchors:
        if generated >= MAX_GENERATED:
            break
        cadence = anchor.recurrence
        duration = anchor.end - anchor.start

        existing_starts = {
            e.start
            for e in Event.query.filter(
                Event.user_id == uid,
                Event.recurrence_parent_id == anchor.id,
                Event.start >= window_start,
                Event.start < window_end,
            ).all()
        }
        # Slots the user deliberately deleted — treated exactly like an
        # "already exists" slot below, so a cancelled occurrence stays
        # gone instead of being refilled the next time this range loads.
        existing_starts |= {
            e.occurrence_start
            for e in EventException.query.filter(
                EventException.anchor_id == anchor.id,
                EventException.occurrence_start >= window_start,
                EventException.occurrence_start < window_end,
            ).all()
        }

        # Fast-forward the cursor to roughly window_start before walking
        # occurrence-by-occurrence — otherwise a daily series created a
        # year ago would replay ~365 steps on every single page load.
        cursor = anchor.start
        if cadence == "daily" and cursor < window_start:
            cursor += timedelta(days=(window_start - cursor).days)
        elif cadence == "weekly" and cursor < window_start:
            cursor += timedelta(weeks=(window_start - cursor).days // 7)
        while cursor < window_start:
            cursor = _advance_recurrence(cursor, cadence)

        while cursor < window_end and generated < MAX_GENERATED:
            if cursor != anchor.start and cursor not in existing_starts:
                db.session.add(Event(
                    user_id=uid,
                    title=anchor.title,
                    start=cursor,
                    end=cursor + duration,
                    color=anchor.color,
                    recurrence_parent_id=anchor.id,
                ))
                generated += 1
            cursor = _advance_recurrence(cursor, cadence)

    if generated:
        db.session.commit()


@dashboard_bp.route("/calendar")
@login_required
def calendar_page():
    uid = current_user.id
    # Right on first render for the common case (an account with a known
    # timezone) instead of relying only on this page's own client-side
    # correction (see calendar.html's localTodayString() logic) to patch
    # it after the fact — that JS correction stays as a safety net for a
    # brand-new account that hasn't reported a timezone yet.
    today = user_today(current_user)

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
        due = next_due_date(tx, today)
        if due is None:
            continue
        entry = {
            "date": due.strftime("%Y-%m-%d"),
            "date_label": f"{cal.month_abbr[due.month]} {due.day}",
            "description": desc,
            "amount": float(tx.amount),
            # The bill's own currency, so its amount is shown exactly as
            # it's actually charged rather than relabeled with "$".
            "currency": tx.native_currency,
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
            Note.archived.is_(False),
        )
        .order_by(Note.pinned.desc(), Note.due_date.asc(), Note.id.asc())
        .all()
    )
    notes_by_date = defaultdict(list)
    for n in notes_due:
        notes_by_date[n.due_date.strftime("%Y-%m-%d")].append(n)

    # Materialize this window's occurrences of any recurring event series
    # before querying — must run first so a freshly-due occurrence is
    # present in the `events` query right below instead of one page load
    # behind.
    _generate_recurring_events(uid, fetch_start_dt, fetch_end_dt)

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

    # One rate fetch (DB-cached, cheap) reused for every day in the grid,
    # rather than one per day.
    calendar_display_currency = current_user.preferred_currency or "USD"
    calendar_rates = get_rates() if calendar_display_currency != "USD" else None

    grid_days = []
    cursor = grid_start
    while cursor <= grid_end:
        key = cursor.strftime("%Y-%m-%d")
        day_txs = by_date.get(key, [])
        # value_in_usd, not amount -- a day's transactions could be in
        # different currencies -- then converted to this user's own
        # display currency, same as every other total on this page.
        net_usd = sum((t.value_in_usd if t.type == "income" else -t.value_in_usd for t in day_txs), Decimal("0"))
        net = convert_usd_to(net_usd, calendar_display_currency, calendar_rates)

        # Each event's own chosen color (same NOTE_COLORS palette as the
        # Add Event modal's swatches), deduped and capped at 3 so a day
        # with many events still reads as a handful of dots, not a smear.
        day_event_colors = []
        for e in events_by_date.get(key, []):
            if e.color not in day_event_colors:
                day_event_colors.append(e.color)
        day_event_colors = day_event_colors[:3]

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
            "event_colors": day_event_colors,
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
                # Native amount + its own currency for the row itself;
                # display_amount (converted to this user's display
                # currency) only for summing a day's Net, since a raw sum
                # across currencies is meaningless.
                "amount": float(t.amount),
                "currency": t.native_currency,
                "display_amount": float(convert_usd_to(t.value_in_usd, calendar_display_currency, calendar_rates)),
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
        key: [_serialize_event(e) for e in day_events]
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
        # Server-rendered fallback symbol for the day-cell net chips, until
        # the page's script reformats them with the real formatter.
        currency_symbol=CURRENCY_SYMBOLS.get(calendar_display_currency, "$"),
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
        "recurrence": event.recurrence,
        "recurrence_parent_id": event.recurrence_parent_id,
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
    recurrence = data.get("recurrence") or None

    if not title:
        return jsonify({"message": "Title is required"}), 400
    if start is None or end is None:
        return jsonify({"message": "Invalid start/end time"}), 400
    if end <= start:
        return jsonify({"message": "End must be after start"}), 400
    if color not in EVENT_COLORS:
        color = "sky"
    if recurrence is not None and recurrence not in EVENT_RECURRENCES:
        recurrence = None

    event = Event(user_id=current_user.id, title=title, start=start, end=end, color=color, recurrence=recurrence)
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

    # Only meaningful on an anchor (a generated occurrence's own
    # recurrence_parent_id is set, and _generate_recurring_events() only
    # ever scans rows where that's None) — silently ignored on a child so
    # the modal can't create a confusing dead "recurrence" value that
    # never actually generates anything.
    if "recurrence" in data and event.recurrence_parent_id is None:
        recurrence = data.get("recurrence") or None
        if recurrence is None or recurrence in EVENT_RECURRENCES:
            event.recurrence = recurrence

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
        if event.recurrence_parent_id is not None:
            # One occurrence of a series — record an exception so
            # _generate_recurring_events() never refills this exact slot
            # again (see EventException's docstring for why that's needed).
            db.session.add(EventException(
                anchor_id=event.recurrence_parent_id,
                occurrence_start=event.start,
            ))
        else:
            # Deleting the anchor takes the whole series with it: no more
            # occurrences will ever be generated once the row carrying the
            # recurrence rule is gone, and leftover children would just be
            # orphaned (their recurrence_parent_id points nowhere) — plus
            # any exceptions recorded against this anchor are now moot.
            Event.query.filter_by(recurrence_parent_id=event.id).delete()
            EventException.query.filter_by(anchor_id=event.id).delete()

        db.session.delete(event)
        db.session.commit()
        return jsonify({"message": "Event deleted"})
    except Exception as e:
        db.session.rollback()
        log.error("Error deleting event: %s", e, exc_info=True)
        return jsonify({"message": "Database error"}), 500


# ---------------------------------------------------------------------------
# Calendar export — a private, per-user iCalendar (.ics) subscription feed.
# One-way (Artha -> whatever calendar app the user picks), not OAuth-based
# two-way sync: a long unguessable token in the URL is the entire auth
# model, the same approach every calendar app's own "private ICS link"
# feature already uses, since a subscribing app fetches this unattended on
# its own schedule with no session cookie to send. Deliberately not
# @login_required for that reason.
# ---------------------------------------------------------------------------

# How far back/forward the feed looks from "today," in the feed owner's
# own timezone. Wide enough that a calendar app's slower refresh cycle
# (Google can take up to a day) still shows a genuinely useful window,
# without generating so many rows the feed gets slow to build.
FEED_WINDOW_PAST_DAYS = 30
FEED_WINDOW_FUTURE_DAYS = 180


@dashboard_bp.route("/calendar/feed/<token>.ics")
def calendar_feed(token):
    owner = User.query.filter_by(calendar_feed_token=token).first()
    if owner is None:
        abort(404)

    today = user_today(owner)
    window_start = today - timedelta(days=FEED_WINDOW_PAST_DAYS)
    window_end = today + timedelta(days=FEED_WINDOW_FUTURE_DAYS)
    window_start_dt = datetime(window_start.year, window_start.month, window_start.day)
    window_end_dt = datetime(window_end.year, window_end.month, window_end.day) + timedelta(days=1)

    cal_feed = ICalendar()
    cal_feed.add("prodid", "-//Artha//Calendar Feed//EN")
    cal_feed.add("version", "2.0")
    # Hints some clients (notably Apple Calendar) use as a display name and
    # a suggested refresh interval — harmless for clients that ignore them.
    cal_feed.add("x-wr-calname", "Artha")
    cal_feed.add("x-wr-timezone", owner.timezone or "UTC")
    cal_feed.add("refresh-interval;value=duration", "PT1H")

    def _add_alarm(component):
        # A subscribing calendar app's own native reminder, independent of
        # Artha's Web Push — this is what actually delivers "reminder
        # syncs to my phone" for a due note/bill, since Google/Apple fire
        # their own local notification off this regardless of whether
        # Artha's own push subscription is even set up on that device.
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", str(component.get("summary")))
        alarm.add("trigger", timedelta(0))
        component.add_component(alarm)

    # Time-blocked events, including this window's recurring occurrences
    # (materialized the same way the calendar page itself triggers it —
    # see _generate_recurring_events's own docstring for why that's
    # necessary rather than optional here).
    _generate_recurring_events(owner.id, window_start_dt, window_end_dt)
    events = Event.query.filter(
        Event.user_id == owner.id,
        Event.start >= window_start_dt,
        Event.start < window_end_dt,
    ).all()
    for event in events:
        ical_event = ICalEvent()
        ical_event.add("uid", f"artha-event-{event.id}@arthaapp.com")
        ical_event.add("summary", event.title)
        ical_event.add("dtstart", event.start)
        ical_event.add("dtend", event.end)
        ical_event.add("dtstamp", datetime.now(timezone.utc))
        _add_alarm(ical_event)
        cal_feed.add_component(ical_event)

    # Notes due within the window (archived notes are excluded the same
    # way calendar_page() excludes them — Trash is only ever reached by
    # trashing an already-archived note, so this one filter also covers
    # trashed notes with no separate deleted_at check needed).
    notes_due = Note.query.filter(
        Note.user_id == owner.id,
        Note.due_date.isnot(None),
        Note.due_date >= window_start,
        Note.due_date <= window_end,
        Note.archived.is_(False),
    ).all()
    for note in notes_due:
        title = note.title or (note.preview[:40] if note.preview else "Untitled note")
        ical_event = ICalEvent()
        ical_event.add("uid", f"artha-note-{note.id}@arthaapp.com")
        ical_event.add("summary", f"{title} due")
        ical_event.add("dtstart", note.due_date)
        ical_event.add("dtstamp", datetime.now(timezone.utc))
        _add_alarm(ical_event)
        cal_feed.add_component(ical_event)

    # Recurring bills' next occurrence inside the window — same
    # most-recent-row-per-(description,type) dedup and next_due_date()
    # walk calendar_page() and cli.py's send_renewal_reminders both
    # already use.
    recurring_rows = Transaction.query.filter_by(user_id=owner.id, is_recurring=True).all()
    templates_by_key = {}
    for t in recurring_rows:
        key = (t.description, t.type)
        current = templates_by_key.get(key)
        if current is None or (t.timestamp and current.timestamp and t.timestamp > current.timestamp):
            templates_by_key[key] = t

    for (desc, _ttype), tx in templates_by_key.items():
        cursor = window_start
        # A single next_due_date() call only finds the *next* occurrence
        # from a given date — walk forward through the whole window so a
        # bill recurring monthly over a 210-day window shows every
        # upcoming occurrence, not just the first.
        seen = set()
        while cursor <= window_end:
            due = next_due_date(tx, cursor)
            if due is None or due > window_end or due in seen:
                break
            seen.add(due)
            ical_event = ICalEvent()
            ical_event.add("uid", f"artha-bill-{tx.id}-{due.isoformat()}@arthaapp.com")
            ical_event.add("summary", f"{desc} due")
            ical_event.add("dtstart", due)
            ical_event.add("dtstamp", datetime.now(timezone.utc))
            _add_alarm(ical_event)
            cal_feed.add_component(ical_event)
            cursor = due + timedelta(days=1)

    # mimetype (not content_type) so Werkzeug appends "; charset=utf-8"
    # itself — passing that charset in-line here too used to produce a
    # doubled "charset=utf-8; charset=utf-8" header.
    response = Response(cal_feed.to_ical(), mimetype="text/calendar")
    response.headers["Content-Disposition"] = 'inline; filename="artha.ics"'
    # Helps Google notice a change sooner within its own throttled
    # subscription refresh window (see this feature's design notes) —
    # this feed is generated fresh on every request, so "now" is always
    # accurate, not just a guess.
    response.headers["Last-Modified"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    return response


@dashboard_bp.route("/calendar/feed/regenerate", methods=["POST"])
@login_required
def regenerate_calendar_feed_token():
    """Issues a fresh, unguessable feed token, invalidating any previous
    one immediately — the only way to revoke a leaked calendar link, since
    the token itself is the entire auth model for /calendar/feed/<token>.ics.
    Also how a token gets created in the first place: there's no token at
    signup, only once this is called for the first time."""
    current_user.calendar_feed_token = secrets.token_urlsafe(32)
    db.session.commit()
    return jsonify({
        "url": url_for("dashboard.calendar_feed", token=current_user.calendar_feed_token, _external=True),
    })


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


# 300 chars is generous for a genuine calculator line (a real word problem
# reads more like a sentence than a paragraph); mainly here to bound
# worst-case cost/latency on a pathological paste, same spirit as
# ai_service.py's own _STATEMENT_MAX_CHARS.
_CALCULATOR_LINE_MAX_CHARS = 300


@dashboard_bp.route("/calculator/solve", methods=["POST"])
@login_required
@ai_limit(AI_CALCULATOR_LIMIT)
def calculator_solve():
    """
    AI fallback for one Smart Calculator line the client-side
    deterministic pipeline (templates/calculator.html) couldn't trust —
    see AIService.solve_calculator_line's docstring for exactly when the
    client calls this. Debouncing, per-line caching, and the concurrency
    cap all live client-side; this route just forwards one short line to
    the model and back. Never called per keystroke.
    """
    from ...services.ai_service import AIService  # local: ai_service imports
    # EVENT_COLORS/EVENT_RECURRENCES from this module, so a top-level
    # import here would be circular — safe once both modules have
    # finished loading.

    data = request.get_json(silent=True) or {}
    line = (data.get("line") or "").strip()
    if not line or len(line) > _CALCULATOR_LINE_MAX_CHARS:
        return jsonify({"solvable": False}), 400

    result = AIService.solve_calculator_line(line)
    if "error" in result:
        log.warning("Calculator AI fallback failed: %s", result["error"])
        return jsonify({"solvable": False}), 503
    return jsonify(result)


# ---------------------------------------------------------------------------
# What's New — a short, plain-language changelog for users, distinct from
# the Artha Logbook (a separate, personal document written for a different
# audience). See artha/changelog.py for the content and the house rule on
# what belongs in each.
# ---------------------------------------------------------------------------

@dashboard_bp.app_context_processor
def inject_changelog_badge():
    """Top-bar badge — same pattern as admin's inject_admin_badge:
    registered as an app-wide context processor (not gated by
    before_request) so it runs on every page, and an unauthenticated
    request never pays for it."""
    if not current_user.is_authenticated:
        return {}
    if not CHANGELOG_ENTRIES:
        return {"has_new_changelog": False}
    latest = datetime.strptime(CHANGELOG_ENTRIES[0]["date"], "%Y-%m-%d").date()
    seen = current_user.changelog_seen_at
    return {"has_new_changelog": seen is None or seen.date() < latest}


@dashboard_bp.route("/whats-new")
@login_required
def whats_new():
    """Visiting this page marks everything as seen, the same "opening it
    clears the badge" convention most real changelog widgets use."""
    current_user.changelog_seen_at = datetime.now(timezone.utc)
    db.session.commit()
    return render_template("whats_new.html", entries=CHANGELOG_ENTRIES)
