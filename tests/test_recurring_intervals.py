"""
Weekly/biweekly recurring transactions and an optional end date --
utils.next_due_date()'s interval-aware branch, and
finance.routes.generate_recurring()'s matching dedup/end-date handling.
The existing monthly path (recurrence_interval left None) is covered
already in test_recurring_bills.py / test_transactions.py and is not
touched by any of this.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from artha.extensions import db
from artha.models import Transaction
from artha.utils import next_due_date


def _make_recurring(user, description, amount, interval, when, end_date=None, ttype="expense"):
    tx = Transaction(
        description=description,
        amount=Decimal(amount),
        type=ttype,
        user_id=user.id,
        is_recurring=True,
        recurrence_interval=interval,
        recurring_end_date=end_date,
        timestamp=datetime(when.year, when.month, when.day, 12, 0, tzinfo=timezone.utc),
    )
    db.session.add(tx)
    db.session.commit()
    return tx


# --- next_due_date(), interval-aware ----------------------------------------

def test_next_due_date_weekly_steps_by_seven_days(app, user):
    anchor = date(2026, 3, 2)  # a Monday
    tx = _make_recurring(user, "Gym", "10", "weekly", anchor)
    assert next_due_date(tx, date(2026, 3, 2)) == date(2026, 3, 2)
    assert next_due_date(tx, date(2026, 3, 3)) == date(2026, 3, 9)
    assert next_due_date(tx, date(2026, 3, 20)) == date(2026, 3, 23)


def test_next_due_date_biweekly_steps_by_fourteen_days(app, user):
    anchor = date(2026, 3, 2)
    tx = _make_recurring(user, "Cleaner", "50", "biweekly", anchor)
    assert next_due_date(tx, date(2026, 3, 3)) == date(2026, 3, 16)
    assert next_due_date(tx, date(2026, 3, 17)) == date(2026, 3, 30)


def test_next_due_date_returns_none_once_past_its_end_date(app, user):
    anchor = date(2026, 3, 2)
    tx = _make_recurring(user, "Gym", "10", "weekly", anchor, end_date=date(2026, 3, 9))
    assert next_due_date(tx, date(2026, 3, 9)) == date(2026, 3, 9)  # inclusive
    assert next_due_date(tx, date(2026, 3, 10)) is None


def test_next_due_date_monthly_series_also_respects_end_date(app, user):
    last_month = date.today().replace(day=1) - timedelta(days=1)
    tx = _make_recurring(user, "Loan", "200", None, last_month, end_date=last_month)
    assert next_due_date(tx, date.today()) is None


# --- generate_recurring(): weekly/biweekly generation -----------------------

def test_generate_recurring_creates_a_weekly_occurrence_when_due(auth_client, user):
    last_week = date.today() - timedelta(days=7)
    _make_recurring(user, "Gym", "10", "weekly", last_week)

    resp = auth_client.post("/finance/generate-recurring")
    assert resp.status_code == 200
    assert resp.get_json()["generated"] == 1

    rows = Transaction.query.filter_by(user_id=user.id, description="Gym").all()
    assert len(rows) == 2
    newest = max(rows, key=lambda r: r.timestamp)
    assert newest.recurrence_interval == "weekly"


def test_generate_recurring_skips_a_weekly_series_not_due_yet(auth_client, user):
    tomorrow = date.today() + timedelta(days=1)
    _make_recurring(user, "Gym", "10", "weekly", tomorrow)

    resp = auth_client.post("/finance/generate-recurring")
    assert resp.get_json() == {"generated": 0, "skipped": 1, "ended": 0}


def test_generate_recurring_does_not_duplicate_the_anchor_row_itself(auth_client, user):
    # The anchor transaction a user just logged by hand already *is* this
    # week's occurrence -- loading /finance right after creating it must
    # not immediately manufacture a second one for the same day.
    _make_recurring(user, "Gym", "10", "weekly", date.today())

    resp = auth_client.post("/finance/generate-recurring")
    assert resp.get_json()["generated"] == 0
    assert Transaction.query.filter_by(user_id=user.id, description="Gym").count() == 1


def test_generate_recurring_is_idempotent_for_weekly(auth_client, user):
    last_week = date.today() - timedelta(days=7)
    _make_recurring(user, "Gym", "10", "weekly", last_week)

    first = auth_client.post("/finance/generate-recurring").get_json()
    second = auth_client.post("/finance/generate-recurring").get_json()
    assert first["generated"] == 1
    assert second["generated"] == 0

    rows = Transaction.query.filter_by(user_id=user.id, description="Gym").count()
    assert rows == 2


def test_weekly_occurrences_in_the_same_calendar_month_dont_collide(app, user):
    # The whole reason weekly dedupes on its own exact occurrence date
    # (recurring_month == that date) rather than "anything this calendar
    # month" the way monthly does -- several real weekly occurrences
    # legitimately land in one month, and the unique constraint on
    # (user_id, description, type, recurring_month) must let them.
    first = date.today().replace(day=8)
    second = first + timedelta(days=7)
    db.session.add(Transaction(
        description="Groceries", amount=Decimal("50"), type="expense", user_id=user.id,
        is_recurring=True, recurrence_interval="weekly", recurring_month=first,
        timestamp=datetime(first.year, first.month, first.day, 12, 0, tzinfo=timezone.utc),
    ))
    db.session.add(Transaction(
        description="Groceries", amount=Decimal("50"), type="expense", user_id=user.id,
        is_recurring=True, recurrence_interval="weekly", recurring_month=second,
        timestamp=datetime(second.year, second.month, second.day, 12, 0, tzinfo=timezone.utc),
    ))
    db.session.commit()  # would raise IntegrityError if these collided

    assert Transaction.query.filter_by(user_id=user.id, description="Groceries").count() == 2


def test_generate_recurring_ends_a_series_past_its_end_date_and_clears_is_recurring(auth_client, user):
    anchor = date.today() - timedelta(days=7)
    tx = _make_recurring(user, "Gym", "10", "weekly", anchor, end_date=anchor)

    resp = auth_client.post("/finance/generate-recurring")
    data = resp.get_json()
    assert data["generated"] == 0
    assert data["ended"] == 1

    db.session.refresh(tx)
    assert tx.is_recurring is False


def test_generate_recurring_ends_a_monthly_series_past_its_end_date(auth_client, user):
    last_month_first = (date.today().replace(day=1) - timedelta(days=1)).replace(day=1)
    tx = _make_recurring(user, "Loan", "200", None, last_month_first, end_date=last_month_first)

    resp = auth_client.post("/finance/generate-recurring")
    data = resp.get_json()
    assert data["generated"] == 0
    assert data["ended"] == 1

    db.session.refresh(tx)
    assert tx.is_recurring is False


# --- add_transaction: setting interval/end date at creation -----------------

def test_add_transaction_captures_recurrence_interval_and_end_date(auth_client, user):
    auth_client.post(
        "/add_transaction",
        data={
            "description": "Gym", "amount": "10", "type": "expense",
            "is_recurring": "1", "recurrence_interval": "weekly", "recurring_end_date": "2027-01-01",
        },
    )
    tx = Transaction.query.filter_by(user_id=user.id, description="Gym").first()
    assert tx.recurrence_interval == "weekly"
    assert tx.recurring_end_date == date(2027, 1, 1)


def test_add_transaction_ignores_recurrence_fields_when_not_marked_recurring(auth_client, user):
    auth_client.post(
        "/add_transaction",
        data={
            "description": "Coffee", "amount": "4", "type": "expense",
            "recurrence_interval": "weekly", "recurring_end_date": "2027-01-01",
        },
    )
    tx = Transaction.query.filter_by(user_id=user.id, description="Coffee").first()
    assert tx.recurrence_interval is None
    assert tx.recurring_end_date is None


def test_add_transaction_rejects_unrecognized_interval_by_falling_back_to_monthly(auth_client, user):
    auth_client.post(
        "/add_transaction",
        data={
            "description": "Rent", "amount": "1000", "type": "expense",
            "is_recurring": "1", "recurrence_interval": "daily",
        },
    )
    tx = Transaction.query.filter_by(user_id=user.id, description="Rent").first()
    assert tx.recurrence_interval is None


# --- update_transaction: editing an existing series -------------------------

def test_update_transaction_changes_interval_and_end_date(auth_client, user):
    tx = _make_recurring(user, "Gym", "10", None, date.today())

    resp = auth_client.post(
        f"/update_transaction/{tx.id}",
        json={"recurrence_interval": "biweekly", "recurring_end_date": "2027-06-01"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["recurrence_interval"] == "biweekly"
    assert body["recurring_end_date_label"] == "ends Jun 01, 2027"

    db.session.refresh(tx)
    assert tx.recurrence_interval == "biweekly"
    assert tx.recurring_end_date == date(2027, 6, 1)


def test_update_transaction_clears_end_date_with_empty_string(auth_client, user):
    tx = _make_recurring(user, "Gym", "10", "weekly", date.today(), end_date=date(2027, 1, 1))

    auth_client.post(f"/update_transaction/{tx.id}", json={"recurring_end_date": ""})

    db.session.refresh(tx)
    assert tx.recurring_end_date is None
    assert tx.recurrence_interval == "weekly"  # untouched, since it wasn't sent


def test_update_transaction_leaves_recurrence_fields_alone_when_not_sent(auth_client, user):
    tx = _make_recurring(user, "Gym", "10", "weekly", date.today(), end_date=date(2027, 1, 1))

    auth_client.post(f"/update_transaction/{tx.id}", json={"description": "Gym membership"})

    db.session.refresh(tx)
    assert tx.recurrence_interval == "weekly"
    assert tx.recurring_end_date == date(2027, 1, 1)
