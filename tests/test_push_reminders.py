from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from artha.cli import _due_today_items
from artha.extensions import db
from artha.models import Note, Transaction
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
