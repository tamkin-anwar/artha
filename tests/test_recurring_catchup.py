"""
Recurring catch-up (generate_due_recurring), user-local "this month", and
Safe to Spend counting every remaining occurrence exactly once.

Before this: a weekly/biweekly occurrence was only ever created if Finance
happened to be opened on its exact due day, a monthly bill was lost for
any month Finance wasn't opened at all, generated copies dropped their
category, and Safe to Spend counted only a bill's *next* occurrence (and
double-counted a monthly bill whose copy for this month already existed).
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from artha.extensions import db
from artha.models import Transaction
from artha.utils import current_month_bounds, recurring_occurrences


def _make_recurring(user, description, amount, when, interval=None, end_date=None, category=None, ttype="expense"):
    tx = Transaction(
        description=description,
        amount=Decimal(amount),
        type=ttype,
        user_id=user.id,
        is_recurring=True,
        recurrence_interval=interval,
        recurring_end_date=end_date,
        category=category,
        timestamp=datetime(when.year, when.month, when.day, 12, 0, tzinfo=timezone.utc),
    )
    db.session.add(tx)
    db.session.commit()
    return tx


def _dates(user, description):
    rows = Transaction.query.filter_by(user_id=user.id, description=description).all()
    return sorted(r.timestamp.date() for r in rows)


def _months_back(d: date, n: int, day: int) -> date:
    idx = d.year * 12 + d.month - 1 - n
    return date(idx // 12, idx % 12 + 1, day)


# --- weekly / biweekly ------------------------------------------------------

def test_weekly_occurrence_is_created_even_when_its_due_day_was_missed(auth_client, user):
    anchor = date.today() - timedelta(days=10)
    _make_recurring(user, "Gym", "10", anchor, interval="weekly")

    data = auth_client.post("/finance/generate-recurring").get_json()

    assert data["generated"] == 1
    assert _dates(user, "Gym") == [anchor, anchor + timedelta(days=7)]


def test_every_missed_weekly_occurrence_lands_on_its_own_date(auth_client, user):
    anchor = date.today() - timedelta(days=22)
    _make_recurring(user, "Gym", "10", anchor, interval="weekly")

    auth_client.post("/finance/generate-recurring")

    assert _dates(user, "Gym") == [anchor + timedelta(days=7 * k) for k in range(4)]
    generated = Transaction.query.filter(
        Transaction.description == "Gym", Transaction.recurring_month.isnot(None)
    ).all()
    assert sorted(t.recurring_month for t in generated) == [anchor + timedelta(days=7 * k) for k in (1, 2, 3)]


def test_missed_biweekly_occurrence_is_created(auth_client, user):
    anchor = date.today() - timedelta(days=20)
    _make_recurring(user, "Cleaner", "60", anchor, interval="biweekly")

    auth_client.post("/finance/generate-recurring")

    assert _dates(user, "Cleaner") == [anchor, anchor + timedelta(days=14)]


def test_catchup_is_idempotent(auth_client, user):
    _make_recurring(user, "Gym", "10", date.today() - timedelta(days=22), interval="weekly")

    first = auth_client.post("/finance/generate-recurring").get_json()
    second = auth_client.post("/finance/generate-recurring").get_json()

    assert first["generated"] == 3
    assert second["generated"] == 0


def test_a_deleted_occurrence_is_never_recreated(auth_client, user):
    anchor = date.today() - timedelta(days=22)
    _make_recurring(user, "Gym", "10", anchor, interval="weekly")
    auth_client.post("/finance/generate-recurring")

    middle = Transaction.query.filter_by(description="Gym", recurring_month=anchor + timedelta(days=14)).one()
    auth_client.post(f"/delete_transaction/{middle.id}", headers={"X-Requested-With": "XMLHttpRequest"})
    auth_client.post("/finance/generate-recurring")

    assert anchor + timedelta(days=14) not in _dates(user, "Gym")


def test_weekly_catchup_is_capped_to_about_a_year(auth_client, user):
    _make_recurring(user, "Old gym", "10", date.today() - timedelta(days=730), interval="weekly")

    auth_client.post("/finance/generate-recurring")

    generated = [d for d in _dates(user, "Old gym") if d > date.today() - timedelta(days=700)]
    assert generated, "should still catch up the last year"
    assert min(generated) >= date.today() - timedelta(days=366)


# --- monthly ----------------------------------------------------------------

def test_monthly_bill_backfills_every_missed_month(auth_client, user):
    today = date.today()
    _make_recurring(user, "Rent", "900", _months_back(today, 3, 10))

    data = auth_client.post("/finance/generate-recurring").get_json()

    assert data["generated"] == 3
    assert _dates(user, "Rent") == [_months_back(today, n, 10) for n in (3, 2, 1, 0)]


def test_monthly_backfill_is_capped_at_twelve_months(auth_client, user):
    _make_recurring(user, "Old rent", "900", _months_back(date.today(), 20, 10))

    data = auth_client.post("/finance/generate-recurring").get_json()

    assert data["generated"] == 12


def test_monthly_backfill_stops_at_the_series_end_date(auth_client, user):
    today = date.today()
    start = _months_back(today, 3, 10)
    end = _months_back(today, 2, 20)  # covers the month after start, not later
    tx = _make_recurring(user, "Loan", "200", start, end_date=end)

    data = auth_client.post("/finance/generate-recurring").get_json()

    assert data["generated"] == 1
    assert data["ended"] == 1
    assert _dates(user, "Loan") == [start, _months_back(today, 2, 10)]
    latest = Transaction.query.filter_by(description="Loan").order_by(Transaction.timestamp.desc()).first()
    assert latest.is_recurring is False
    db.session.refresh(tx)
    assert tx.is_recurring is True  # only the series' latest row is retired


def test_generated_copies_keep_the_templates_category(auth_client, user):
    _make_recurring(user, "Spotify", "11", _months_back(date.today(), 1, 5), category="Subscriptions")

    auth_client.post("/finance/generate-recurring")

    copies = Transaction.query.filter(
        Transaction.description == "Spotify", Transaction.recurring_month.isnot(None)
    ).all()
    assert copies and all(c.category == "Subscriptions" for c in copies)


# --- Dashboard triggers the same catch-up ----------------------------------

def test_dashboard_load_creates_due_recurring_bills(auth_client, user):
    anchor = date.today() - timedelta(days=10)
    _make_recurring(user, "Gym", "10", anchor, interval="weekly")

    assert auth_client.get("/").status_code == 200

    assert anchor + timedelta(days=7) in _dates(user, "Gym")


# --- recurring_occurrences --------------------------------------------------

def test_recurring_occurrences_lists_every_weekly_date_in_range(app, user):
    anchor = date(2026, 9, 7)
    tx = _make_recurring(user, "Gym", "10", anchor, interval="weekly")

    assert recurring_occurrences(tx, date(2026, 9, 8), date(2026, 9, 30)) == [
        date(2026, 9, 14), date(2026, 9, 21), date(2026, 9, 28),
    ]


# --- Safe to Spend -----------------------------------------------------------

def _safe_value(body: str) -> str:
    marker = '<div class="eyebrow" style="margin-bottom:8px;">Safe to Spend</div>'
    after = body.split(marker, 1)[1]
    return after.split('data-money-value="', 1)[1].split('"', 1)[0]


def test_safe_to_spend_subtracts_every_remaining_weekly_occurrence(auth_client, user):
    today = date.today()
    auth_client.post("/finance/budget", data={"monthly_cap": "2000"})
    _make_recurring(user, "Gym", "10", today, interval="weekly")

    month_end = current_month_bounds(today)[1].date() - timedelta(days=1)
    remaining = len([d for d in (today + timedelta(days=7 * k) for k in range(1, 6)) if d <= month_end])

    body = auth_client.get("/").get_data(as_text=True)

    # 2000 cap - 10 (today's occurrence, already spent) - 10 per remaining one
    assert float(_safe_value(body)) == 2000 - 10 - 10 * remaining


def test_safe_to_spend_does_not_double_count_a_monthly_bill_already_generated(auth_client, user):
    today = date.today()
    target_day = today.day + 1
    if target_day > 28:
        pytest.skip("Needs a day-of-month still ahead of today that exists in every month.")
    auth_client.post("/finance/budget", data={"monthly_cap": "2000"})
    _make_recurring(user, "Rent", "300", _months_back(today, 1, target_day))

    # Finance first: creates this month's future-dated Rent copy, which is
    # already inside "spent". The Dashboard must not subtract it again.
    auth_client.post("/finance/generate-recurring")
    body = auth_client.get("/").get_data(as_text=True)

    assert float(_safe_value(body)) == 1700.0
    assert "in upcoming bills" in body


# --- user-local "this month" -------------------------------------------------

def test_current_month_bounds_follow_the_date_passed_in():
    start, end = current_month_bounds(date(2026, 9, 30))
    assert (start, end) == (datetime(2026, 9, 1), datetime(2026, 10, 1))


def test_dashboard_month_follows_the_users_own_date_not_the_servers(auth_client, user):
    today = date.today()
    last_month_day = today.replace(day=1) - timedelta(days=1)
    db.session.add(Transaction(
        description="Late paycheck", amount=Decimal("123.00"), type="income", user_id=user.id,
        timestamp=datetime(last_month_day.year, last_month_day.month, last_month_day.day, 12, 0, tzinfo=timezone.utc),
    ))
    db.session.commit()

    # The user's own clock still says it's the last day of last month (the
    # server's may already be in the next one) -- this month's cards and
    # the live-refresh endpoint must both show last month's income.
    with patch("artha.blueprints.dashboard.routes.user_today", return_value=last_month_day), \
         patch("artha.blueprints.finance.routes.user_today", return_value=last_month_day):
        body = auth_client.get("/").get_data(as_text=True)
        totals = auth_client.get("/api/finance_totals").get_json()

    assert 'data-money-value="123.0"' in body
    assert totals["income"] == 123.0


def test_transaction_with_no_date_lands_on_the_users_local_day(auth_client, user):
    user.timezone = "Pacific/Kiritimati"  # UTC+14: often a different date than UTC
    db.session.commit()

    auth_client.post("/add_transaction", data={"description": "Coffee", "amount": "4", "type": "expense"},
                     headers={"X-Requested-With": "XMLHttpRequest"})

    tx = Transaction.query.filter_by(description="Coffee").one()
    assert tx.timestamp.date() == datetime.now(ZoneInfo("Pacific/Kiritimati")).date()
