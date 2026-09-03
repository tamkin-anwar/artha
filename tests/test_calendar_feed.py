"""/calendar/feed/<token>.ics -- the private ICS subscription feed, and
/calendar/feed/regenerate, the login-required route that issues/rotates
the token. See artha/blueprints/dashboard/routes.py's own comments for
the design (one-way, token-is-the-auth, no @login_required on the feed
route itself since a subscribing calendar app has no session cookie)."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from icalendar import Calendar

from artha.extensions import db
from artha.models import Event, Note, Transaction


def test_feed_404s_on_unknown_token(client):
    resp = client.get("/calendar/feed/not-a-real-token.ics")
    assert resp.status_code == 404


def test_feed_404s_when_user_has_no_token_yet(client, user):
    # user.calendar_feed_token is None until first requested -- confirms
    # there's no way to guess/derive a working URL for an account that's
    # never generated one.
    resp = client.get("/calendar/feed/anything.ics")
    assert resp.status_code == 404


def test_feed_requires_no_login_but_regenerate_does(client, user):
    user.calendar_feed_token = "known-test-token"
    db.session.commit()

    # No session cookie at all -- this is the whole point, a calendar app
    # fetching on a timer has none to send.
    resp = client.get("/calendar/feed/known-test-token.ics")
    assert resp.status_code == 200

    resp = client.post("/calendar/feed/regenerate")
    assert resp.status_code in (302, 401)  # redirected to login (or 401), not allowed through


def test_regenerate_issues_a_token_and_returns_its_url(auth_client, user):
    assert user.calendar_feed_token is None

    resp = auth_client.post("/calendar/feed/regenerate")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["url"].endswith(".ics")

    db.session.refresh(user)
    assert user.calendar_feed_token is not None
    assert user.calendar_feed_token in data["url"]


def test_regenerate_rotates_an_existing_token(auth_client, user):
    user.calendar_feed_token = "old-token"
    db.session.commit()

    resp = auth_client.post("/calendar/feed/regenerate")
    db.session.refresh(user)

    assert resp.status_code == 200
    assert user.calendar_feed_token != "old-token"


def test_feed_is_valid_icalendar_output(client, user):
    user.calendar_feed_token = "parse-me"
    db.session.commit()

    resp = client.get("/calendar/feed/parse-me.ics")
    assert resp.status_code == 200
    assert resp.mimetype == "text/calendar"
    # .mimetype strips parameters, so it alone wouldn't catch a doubled
    # "charset=utf-8; charset=utf-8" header -- check the raw header too.
    assert resp.headers["Content-Type"] == "text/calendar; charset=utf-8"

    # Round-trips through the real icalendar parser without raising --
    # the strongest possible proof this is well-formed RFC 5545, not just
    # "looks right" by eyeballing the text.
    parsed = Calendar.from_ical(resp.data)
    assert parsed.get("version") == "2.0"


def test_feed_includes_event_note_and_recurring_bill_inside_the_window(client, user):
    user.calendar_feed_token = "full-feed"
    db.session.commit()

    today = date(2026, 6, 15)

    event = Event(
        user_id=user.id, title="Dentist appointment",
        start=datetime(2026, 6, 16, 14, 0), end=datetime(2026, 6, 16, 15, 0),
    )
    note = Note(title="Renew passport", content="x", user_id=user.id, due_date=date(2026, 6, 20))
    # Last occurrence one month before "today" -- next_due_date() rolls
    # this forward to land inside the window, same fixture shape
    # test_push_reminders.py's recurring-bill tests already use.
    bill = Transaction(
        description="Rent", amount=Decimal("1500"), type="expense", user_id=user.id,
        is_recurring=True, timestamp=datetime(2026, 5, 15, 12, 0),
    )
    db.session.add_all([event, note, bill])
    db.session.commit()

    with patch("artha.blueprints.dashboard.routes.user_today", return_value=today):
        resp = client.get("/calendar/feed/full-feed.ics")

    assert resp.status_code == 200
    parsed = Calendar.from_ical(resp.data)
    vevents = list(parsed.walk("VEVENT"))
    summaries = {str(e.get("summary")) for e in vevents}

    assert "Dentist appointment" in summaries
    assert "Renew passport due" in summaries
    assert "Rent due" in summaries
    # Every VEVENT gets a VALARM so a subscribing app's own native
    # notification fires -- the mechanism that actually delivers "syncs
    # to my phone" for a due date, independent of Artha's own Web Push.
    for e in vevents:
        assert len(e.walk("VALARM")) == 1


def test_feed_excludes_items_outside_the_window(client, user):
    user.calendar_feed_token = "windowed-feed"
    db.session.commit()

    today = date(2026, 6, 15)
    from artha.blueprints.dashboard.routes import FEED_WINDOW_FUTURE_DAYS

    too_far = today + timedelta(days=FEED_WINDOW_FUTURE_DAYS + 30)
    note_in = Note(title="Inside the window", content="x", user_id=user.id, due_date=today + timedelta(days=5))
    note_out = Note(title="Outside the window", content="x", user_id=user.id, due_date=too_far)
    db.session.add_all([note_in, note_out])
    db.session.commit()

    with patch("artha.blueprints.dashboard.routes.user_today", return_value=today):
        resp = client.get("/calendar/feed/windowed-feed.ics")

    parsed = Calendar.from_ical(resp.data)
    summaries = {str(e.get("summary")) for e in parsed.walk("VEVENT")}
    assert "Inside the window due" in summaries
    assert "Outside the window due" not in summaries


def test_feed_excludes_archived_notes(client, user):
    user.calendar_feed_token = "no-archived"
    db.session.commit()

    today = date(2026, 6, 15)
    note = Note(
        title="Done already", content="x", user_id=user.id,
        due_date=today, archived=True,
    )
    db.session.add(note)
    db.session.commit()

    with patch("artha.blueprints.dashboard.routes.user_today", return_value=today):
        resp = client.get("/calendar/feed/no-archived.ics")

    parsed = Calendar.from_ical(resp.data)
    summaries = {str(e.get("summary")) for e in parsed.walk("VEVENT")}
    assert "Done already due" not in summaries
