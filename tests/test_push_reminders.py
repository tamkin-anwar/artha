from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from artha.cli import _due_today_items
from artha.extensions import db
from artha.models import Event, Note, Transaction
from artha.models.push_subscription import PushSubscription


def _recurring_tx(user, description, days_ago_last_occurrence):
    last = date.today() - timedelta(days=days_ago_last_occurrence)
    tx = Transaction(
        description=description,
        amount=Decimal("10"),
        type="expense",
        user_id=user.id,
        is_recurring=True,
        timestamp=datetime(last.year, last.month, last.day, 12, 0, tzinfo=timezone.utc),
    )
    db.session.add(tx)
    db.session.commit()
    return tx


def test_due_today_items_includes_bill_due_today(app, user):
    # Last occurrence exactly 30 days ago lands back on roughly today for
    # most months — use the same-day-last-month trick the other recurring
    # tests use instead, for a guaranteed hit regardless of month length.
    today = date.today()
    year, month = today.year, today.month - 1 or 12
    if today.month == 1:
        year -= 1
    tx = Transaction(
        description="Netflix",
        amount=Decimal("15.99"),
        type="expense",
        user_id=user.id,
        is_recurring=True,
        timestamp=datetime(year, month, today.day, 12, 0, tzinfo=timezone.utc),
    )
    db.session.add(tx)
    db.session.commit()

    items = _due_today_items(user.id, today)
    assert items == ["Netflix"]


def test_due_today_items_includes_note_due_today(app, user):
    note = Note(title="Renew passport", content="x", user_id=user.id, due_date=date.today())
    db.session.add(note)
    db.session.commit()

    items = _due_today_items(user.id, date.today())
    assert items == ["Renew passport"]


def test_due_today_items_note_falls_back_to_preview_when_untitled(app, user):
    note = Note(content="x", preview="Call the vet about the appointment", user_id=user.id, due_date=date.today())
    db.session.add(note)
    db.session.commit()

    items = _due_today_items(user.id, date.today())
    assert items == ["Call the vet about the appointment"]


def test_due_today_items_ignores_notes_due_other_days(app, user):
    note = Note(title="Not yet", content="x", user_id=user.id, due_date=date.today() + timedelta(days=3))
    db.session.add(note)
    db.session.commit()

    assert _due_today_items(user.id, date.today()) == []


def test_due_today_items_ignores_archived_notes(app, user):
    note = Note(title="Done with this", content="x", user_id=user.id, due_date=date.today(), archived=True)
    db.session.add(note)
    db.session.commit()

    assert _due_today_items(user.id, date.today()) == []


def test_due_today_items_excludes_bill_when_notify_bills_off(app, user):
    today = date.today()
    year, month = today.year, today.month - 1 or 12
    if today.month == 1:
        year -= 1
    db.session.add(Transaction(
        description="Netflix", amount=Decimal("15.99"), type="expense", user_id=user.id, is_recurring=True,
        timestamp=datetime(year, month, today.day, 12, 0, tzinfo=timezone.utc),
    ))
    db.session.add(Note(title="Renew passport", content="x", user_id=user.id, due_date=today))
    db.session.commit()

    items = _due_today_items(user.id, today, notify_bills=False, notify_notes=True)
    assert items == ["Renew passport"]


def test_due_today_items_excludes_notes_when_notify_notes_off(app, user):
    today = date.today()
    year, month = today.year, today.month - 1 or 12
    if today.month == 1:
        year -= 1
    db.session.add(Transaction(
        description="Netflix", amount=Decimal("15.99"), type="expense", user_id=user.id, is_recurring=True,
        timestamp=datetime(year, month, today.day, 12, 0, tzinfo=timezone.utc),
    ))
    db.session.add(Note(title="Renew passport", content="x", user_id=user.id, due_date=today))
    db.session.commit()

    items = _due_today_items(user.id, today, notify_bills=True, notify_notes=False)
    assert items == ["Netflix"]


def test_send_renewal_reminders_respects_notify_bills_due_false(app, user):
    today = date.today()
    year, month = today.year, today.month - 1 or 12
    if today.month == 1:
        year -= 1
    db.session.add(Transaction(
        description="Rent", amount=Decimal("1500"), type="expense", user_id=user.id, is_recurring=True,
        timestamp=datetime(year, month, today.day, 12, 0, tzinfo=timezone.utc),
    ))
    db.session.add(Note(title="Call the dentist", content="x", user_id=user.id, due_date=today))
    db.session.add(PushSubscription(user_id=user.id, endpoint="https://example.com/y", p256dh="a", auth="b"))
    user.notify_bills_due = False
    db.session.commit()

    sent_calls = []

    def fake_send_push(sub, title, body, url="/"):
        sent_calls.append({"body": body})
        return "sent"

    with patch("artha.cli.send_push", side_effect=fake_send_push):
        result = app.test_cli_runner().invoke(args=["send-renewal-reminders"])

    assert result.exit_code == 0
    assert len(sent_calls) == 1
    assert "Rent" not in sent_calls[0]["body"]
    assert "Call the dentist" in sent_calls[0]["body"]


def test_due_today_items_includes_event_starting_today(app, user):
    today = date.today()
    start = datetime(today.year, today.month, today.day, 14, 0)
    event = Event(user_id=user.id, title="Dentist appointment", start=start, end=start + timedelta(hours=1))
    db.session.add(event)
    db.session.commit()

    items = _due_today_items(user.id, today)
    assert items == ["Dentist appointment"]


def test_due_today_items_ignores_events_on_other_days(app, user):
    today = date.today()
    start = datetime(today.year, today.month, today.day, 14, 0) + timedelta(days=2)
    db.session.add(Event(user_id=user.id, title="Later this week", start=start, end=start + timedelta(hours=1)))
    db.session.commit()

    assert _due_today_items(user.id, date.today()) == []


def test_due_today_items_excludes_events_when_notify_events_off(app, user):
    today = date.today()
    start = datetime(today.year, today.month, today.day, 9, 0)
    db.session.add(Event(user_id=user.id, title="Standup", start=start, end=start + timedelta(minutes=30)))
    db.session.add(Note(title="Renew passport", content="x", user_id=user.id, due_date=today))
    db.session.commit()

    items = _due_today_items(user.id, today, notify_bills=True, notify_notes=True, notify_events=False)
    assert items == ["Renew passport"]


def test_due_today_items_materializes_recurring_event_for_today(app, user):
    # Anchor created 3 days ago, daily recurrence — nobody has loaded the
    # calendar since, so today's occurrence doesn't exist as a real Event
    # row yet. _due_today_items must materialize it itself before querying,
    # same as the calendar page does on load.
    today = date.today()
    anchor_start = datetime(today.year, today.month, today.day, 8, 0) - timedelta(days=3)
    anchor = Event(
        user_id=user.id,
        title="Morning standup",
        start=anchor_start,
        end=anchor_start + timedelta(minutes=15),
        recurrence="daily",
    )
    db.session.add(anchor)
    db.session.commit()

    assert Event.query.filter_by(user_id=user.id).count() == 1

    items = _due_today_items(user.id, today)
    assert items == ["Morning standup"]
    assert Event.query.filter_by(user_id=user.id).count() > 1


def test_send_renewal_reminders_respects_notify_events_due_false(app, user):
    today = date.today()
    start = datetime(today.year, today.month, today.day, 10, 0)
    db.session.add(Event(user_id=user.id, title="Team sync", start=start, end=start + timedelta(hours=1)))
    db.session.add(Note(title="Call the dentist", content="x", user_id=user.id, due_date=today))
    db.session.add(PushSubscription(user_id=user.id, endpoint="https://example.com/z", p256dh="a", auth="b"))
    user.notify_events_due = False
    db.session.commit()

    sent_calls = []

    def fake_send_push(sub, title, body, url="/"):
        sent_calls.append({"body": body})
        return "sent"

    with patch("artha.cli.send_push", side_effect=fake_send_push):
        result = app.test_cli_runner().invoke(args=["send-renewal-reminders"])

    assert result.exit_code == 0
    assert len(sent_calls) == 1
    assert "Team sync" not in sent_calls[0]["body"]
    assert "Call the dentist" in sent_calls[0]["body"]


def test_send_renewal_reminders_combines_bill_and_note_in_one_push(app, user):
    today = date.today()
    year, month = today.year, today.month - 1 or 12
    if today.month == 1:
        year -= 1
    db.session.add(Transaction(
        description="Rent", amount=Decimal("1500"), type="expense", user_id=user.id, is_recurring=True,
        timestamp=datetime(year, month, today.day, 12, 0, tzinfo=timezone.utc),
    ))
    db.session.add(Note(title="Call the dentist", content="x", user_id=user.id, due_date=today))
    db.session.add(PushSubscription(user_id=user.id, endpoint="https://example.com/x", p256dh="a", auth="b"))
    db.session.commit()

    sent_calls = []

    def fake_send_push(sub, title, body, url="/"):
        sent_calls.append({"title": title, "body": body})
        return "sent"

    with patch("artha.cli.send_push", side_effect=fake_send_push):
        result = app.test_cli_runner().invoke(args=["send-renewal-reminders"])

    assert result.exit_code == 0
    assert len(sent_calls) == 1
    assert sent_calls[0]["title"] == "Due today"
    assert "Rent" in sent_calls[0]["body"]
    assert "Call the dentist" in sent_calls[0]["body"]
