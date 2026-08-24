from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from artha.extensions import db
from artha.models import Transaction


def _make_recurring(user, description, amount, ttype="expense", category=None, day_offset=0):
    """day_offset shifts the day-of-month next_due_date() reads (0 = today,
    negative = already passed this month so it rolls to next month,
    positive = still upcoming this month) — same signal the app itself
    uses, since there's no explicit due-day field."""
    when = date.today() + timedelta(days=day_offset)
    tx = Transaction(
        description=description,
        amount=Decimal(amount),
        type=ttype,
        user_id=user.id,
        is_recurring=True,
        category=category,
        timestamp=datetime(when.year, when.month, when.day, 12, 0, tzinfo=timezone.utc),
    )
    db.session.add(tx)
    db.session.commit()
    return tx


def test_recurring_panel_absent_with_no_recurring_transactions(auth_client, user):
    body = auth_client.get("/finance").get_data(as_text=True)
    # ".recurring-toggle-btn" (the CSS rule) is always in the page's <style>
    # block regardless — check for the actual button element instead.
    assert 'class="recurring-toggle-btn"' not in body


def test_recurring_panel_lists_bill_with_amount_and_category(auth_client, user):
    _make_recurring(user, "Netflix", "15.99", category="subscriptions")
    body = auth_client.get("/finance").get_data(as_text=True)

    assert "recurring-toggle-btn" in body
    assert "Netflix" in body
    assert "15.99" in body
    assert "Subscriptions" in body


def test_recurring_panel_shows_income_bills_too(auth_client, user):
    _make_recurring(user, "Freelance retainer", "500.00", ttype="income", category="income")
    body = auth_client.get("/finance").get_data(as_text=True)
    assert "Freelance retainer" in body
    assert "+$500.00" in body


def test_recurring_panel_sorts_soonest_due_first(auth_client, user):
    # Already passed this month -> rolls to next month's occurrence.
    _make_recurring(user, "Later Bill", "10.00", day_offset=-1)
    # Still upcoming this month -> due sooner than "Later Bill" above.
    _make_recurring(user, "Sooner Bill", "20.00", day_offset=1)

    body = auth_client.get("/finance").get_data(as_text=True)
    assert body.index("Sooner Bill") < body.index("Later Bill")


def test_recurring_panel_flags_bill_due_within_a_week(auth_client, user):
    _make_recurring(user, "Due Today", "9.99", day_offset=0)
    body = auth_client.get("/finance").get_data(as_text=True)
    assert "recurring-due-soon" in body
    assert "Today" in body


def test_recurring_panel_uses_most_recent_row_per_rule(auth_client, user):
    """Same recurring rule accumulates one row per month (generate_recurring)
    — the panel should show the latest amount/category, not an older one,
    same "most recent wins" dedup already used for recurring_count."""
    older = _make_recurring(user, "Gym", "40.00", category="health", day_offset=-40)
    older.timestamp = older.timestamp.replace(year=older.timestamp.year - 1)
    db.session.commit()
    _make_recurring(user, "Gym", "45.00", category="health", day_offset=0)

    body = auth_client.get("/finance").get_data(as_text=True)
    assert "45.00" in body
    assert "40.00" not in body
