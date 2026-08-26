from flask import jsonify, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import or_

from ...models import Event, Note, Transaction
from ...models.scenario import Scenario
from . import search_bp


def _fmt_date(dt) -> str:
    return dt.strftime("%b %d, %Y") if dt else ""


def _fmt_datetime(dt) -> str:
    # Manual 12-hour formatting rather than %-I/%-d — those are
    # glibc/macOS-only strftime extensions, not portable (see the same
    # pattern already used for event times in dashboard/routes.py).
    if not dt:
        return ""
    hour12 = dt.hour % 12 or 12
    return f"{dt.strftime('%b %d, %Y')} · {hour12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"

# Fixed cap per category — this powers a quick-jump ⌘K palette, not a
# full search-results page, so a bounded list of the most relevant hits
# per type is more useful than an exhaustive one to scroll through. Was
# 5; raised to 8 since 5 was clipping common searches before the user
# could find what they were looking for.
MAX_RESULTS_PER_CATEGORY = 8
MIN_QUERY_LENGTH = 2


@search_bp.get("/search")
@login_required
def search():
    q = (request.args.get("q") or "").strip()
    if len(q) < MIN_QUERY_LENGTH:
        return jsonify({"notes": [], "transactions": [], "scenarios": [], "events": []})

    uid = current_user.id
    like = f"%{q}%"

    notes = (
        Note.query.filter(
            Note.user_id == uid,
            Note.archived.is_(False),
            Note.deleted_at.is_(None),
            or_(Note.title.ilike(like), Note.content.ilike(like), Note.tag.ilike(like)),
        )
        .order_by(Note.updated_at.desc())
        .limit(MAX_RESULTS_PER_CATEGORY)
        .all()
    )

    transactions = (
        Transaction.query.filter(
            Transaction.user_id == uid,
            # Category included alongside description — searching "groceries"
            # should surface transactions filed under that category even
            # when the word never appears in the description itself.
            or_(Transaction.description.ilike(like), Transaction.category.ilike(like)),
        )
        .order_by(Transaction.timestamp.desc())
        .limit(MAX_RESULTS_PER_CATEGORY)
        .all()
    )

    scenarios = (
        Scenario.query.filter(
            Scenario.user_id == uid,
            Scenario.status != "archived",
            or_(Scenario.title.ilike(like), Scenario.description.ilike(like), Scenario.notes.ilike(like)),
        )
        .order_by(Scenario.created_at.desc())
        .limit(MAX_RESULTS_PER_CATEGORY)
        .all()
    )

    events = (
        Event.query.filter(
            Event.user_id == uid,
            Event.title.ilike(like),
        )
        .order_by(Event.start.desc())
        .limit(MAX_RESULTS_PER_CATEGORY)
        .all()
    )

    return jsonify({
        "notes": [
            {
                "id": n.id,
                "title": n.title or "Untitled",
                "snippet": (n.preview or "")[:120],
                # Notes' own client-side JS already reads ?open=<id> to
                # auto-open a note on load (same convention Dashboard's
                # "Today" panel links use).
                "url": url_for("notes.notes_page") + f"?open={n.id}",
            }
            for n in notes
        ],
        "transactions": [
            {
                "id": t.id,
                "title": t.description,
                "snippet": f"{'+' if t.type == 'income' else '-'}${t.amount:.2f} · {_fmt_date(t.timestamp)}",
                # No per-transaction deep link exists — the closest useful
                # target is the month it's actually in.
                "url": url_for("finance.finance_page", month=(t.timestamp.strftime("%Y-%m") if t.timestamp else None)),
            }
            for t in transactions
        ],
        "scenarios": [
            {
                "id": s.id,
                "title": s.title,
                "snippet": s.category or "",
                "url": url_for("scenarios.detail", scenario_id=s.id),
            }
            for s in scenarios
        ],
        "events": [
            {
                "id": e.id,
                "title": e.title,
                "snippet": _fmt_datetime(e.start),
                # Same reasoning as transactions above — gets you to the
                # right month, not a specific day/event highlight.
                "url": url_for("dashboard.calendar_page", month=(e.start.strftime("%Y-%m") if e.start else None)),
            }
            for e in events
        ],
    })
