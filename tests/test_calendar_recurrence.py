from datetime import date, datetime
from unittest.mock import patch

from artha.extensions import db
from artha.models import Event, EventException


def _make_anchor(user, start, end, cadence="weekly", title="Team sync", color="sky"):
    event = Event(
        user_id=user.id, title=title, start=start, end=end,
        color=color, recurrence=cadence,
    )
    db.session.add(event)
    db.session.commit()
    return event


def test_visiting_dashboard_generates_todays_recurring_occurrence(app, auth_client, user):
    # A daily series starting well before the mocked "today" below, so
    # today's occurrence is real but nothing has materialized it yet —
    # nobody in this test has visited /calendar, which is the only other
    # place that call happens.
    anchor = _make_anchor(user, datetime(2026, 1, 1, 9, 0), datetime(2026, 1, 1, 9, 30), cadence="daily")

    with patch("artha.blueprints.dashboard.routes.user_today", return_value=date(2026, 3, 15)):
        resp = auth_client.get("/")

    assert resp.status_code == 200
    assert "Team sync" in resp.get_data(as_text=True)
    generated = Event.query.filter_by(
        recurrence_parent_id=anchor.id, start=datetime(2026, 3, 15, 9, 0)
    ).first()
    assert generated is not None


def test_viewing_calendar_generates_occurrences(app, auth_client, user):
    anchor = _make_anchor(user, datetime(2026, 8, 4, 10, 0), datetime(2026, 8, 4, 10, 30))

    auth_client.get("/calendar?month=2026-08")

    children = Event.query.filter_by(recurrence_parent_id=anchor.id).all()
    starts = {e.start.strftime("%Y-%m-%d") for e in children}
    assert "2026-08-11" in starts
    assert "2026-08-18" in starts
    assert "2026-08-25" in starts


def test_deleting_one_occurrence_does_not_regenerate_it(app, auth_client, user):
    anchor = _make_anchor(user, datetime(2026, 8, 4, 10, 0), datetime(2026, 8, 4, 10, 30))

    # First view materializes the month's occurrences.
    auth_client.get("/calendar?month=2026-08")
    aug_11 = Event.query.filter_by(recurrence_parent_id=anchor.id, start=datetime(2026, 8, 11, 10, 0)).first()
    assert aug_11 is not None

    resp = auth_client.post(f"/calendar/events/{aug_11.id}/delete")
    assert resp.status_code == 200
    assert db.session.get(Event, aug_11.id) is None
    assert EventException.query.filter_by(anchor_id=anchor.id, occurrence_start=datetime(2026, 8, 11, 10, 0)).count() == 1

    # Revisiting the same month is exactly what used to regenerate the
    # deleted occurrence — confirm it now stays gone.
    auth_client.get("/calendar?month=2026-08")
    assert Event.query.filter_by(recurrence_parent_id=anchor.id, start=datetime(2026, 8, 11, 10, 0)).first() is None

    # The rest of the series is untouched.
    aug_18 = Event.query.filter_by(recurrence_parent_id=anchor.id, start=datetime(2026, 8, 18, 10, 0)).first()
    assert aug_18 is not None


def test_deleting_the_anchor_removes_the_whole_series(app, auth_client, user):
    anchor = _make_anchor(user, datetime(2026, 8, 4, 10, 0), datetime(2026, 8, 4, 10, 30))
    anchor_id = anchor.id

    auth_client.get("/calendar?month=2026-08")
    assert Event.query.filter_by(recurrence_parent_id=anchor_id).count() > 0

    resp = auth_client.post(f"/calendar/events/{anchor_id}/delete")
    assert resp.status_code == 200
    assert db.session.get(Event, anchor_id) is None
    assert Event.query.filter_by(recurrence_parent_id=anchor_id).count() == 0
    assert EventException.query.filter_by(anchor_id=anchor_id).count() == 0
