"""services/month_plan.py: this month's bills and what someone usually
spends, checked against fixed dates so the results don't depend on the
day the suite runs."""

from datetime import date, datetime, timezone
from decimal import Decimal

from artha.extensions import db
from artha.models import Transaction
from artha.models.budget import Budget
from artha.services.month_plan import bills_this_month, typical_spending
from artha.services.month_snapshot import build_month_snapshot

TODAY = date(2026, 3, 15)


def _add(user, description, amount, day, *, category=None, recurring=False, interval=None, t_type="expense"):
    tx = Transaction(
        user_id=user.id, description=description, amount=Decimal(amount), type=t_type,
        category=category, currency="USD", usd_value=Decimal(amount),
        timestamp=datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc),
        is_recurring=recurring, recurrence_interval=interval,
    )
    db.session.add(tx)
    db.session.commit()
    return tx


def test_bills_split_into_paid_scheduled_and_due_matching_safe_to_spend(user):
    db.session.add(Budget(user_id=user.id, monthly_cap=Decimal("2000"), currency="USD"))
    _add(user, "Rent", "1000.00", date(2026, 3, 1), category="housing", recurring=True)
    _add(user, "Gym", "50.00", date(2026, 2, 20), category="health", recurring=True)
    _add(user, "Netflix", "15.00", date(2026, 3, 25), category="subscriptions", recurring=True)
    _add(user, "Lunch club", "10.00", date(2026, 3, 10), category="dining", recurring=True, interval="weekly")
    _add(user, "Coffee", "5.00", date(2026, 3, 12), category="dining")

    snap = build_month_snapshot(user, today=TODAY)
    result = bills_this_month(snap)

    assert [(b.description, b.due.day, b.status) for b in result.bills] == [
        ("Rent", 1, "paid"),
        ("Lunch club", 10, "paid"),
        ("Lunch club", 17, "due"),
        ("Gym", 20, "due"),
        ("Lunch club", 24, "due"),
        ("Netflix", 25, "scheduled"),
        ("Lunch club", 31, "due"),
    ]
    assert result.posted_usd == Decimal("1025.00")
    assert result.unposted_usd == Decimal("80.00")
    assert result.paid_usd == Decimal("1010.00")
    assert result.coming_usd == Decimal("95.00")

    # The plan is Safe to Spend in parts: cap - bills - everyday == left.
    assert Decimal(str(snap.safe_to_spend_unposted)) == result.unposted_usd
    everyday = snap.expense - result.posted_usd
    assert everyday == Decimal("5.00")
    assert Decimal("2000") - result.total_usd - everyday == Decimal(str(snap.safe_to_spend))


def test_typical_spending_averages_only_months_with_spending(user):
    _add(user, "Way back", "999.00", date(2025, 11, 5), category="dining")
    _add(user, "Dinner", "100.00", date(2026, 1, 10), category="dining")
    _add(user, "Dinner", "200.00", date(2026, 2, 10), category="dining")
    _add(user, "Groceries", "60.00", date(2026, 2, 11), category="groceries")
    _add(user, "This month", "500.00", date(2026, 3, 2), category="dining")
    _add(user, "Paycheck", "4000.00", date(2026, 2, 1), category="income", t_type="income")

    typical = typical_spending(user, TODAY, "USD", None)

    assert typical["months"] == 2
    assert typical["overall"] == Decimal("180.00")
    assert typical["by_category"] == {"dining": Decimal("150.00"), "groceries": Decimal("30.00")}


def test_typical_spending_is_none_without_history(user):
    _add(user, "This month", "500.00", date(2026, 3, 2), category="dining")
    assert typical_spending(user, TODAY, "USD", None) is None
