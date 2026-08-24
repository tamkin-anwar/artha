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


def _recurring(client, period="month", **params):
    params = {"view": "recurring", "period": period, **params}
    return client.get("/finance/breakdown", query_string=params).get_json()


# --- Transactions tab: the jump-link into the Recurring tab -----------------

def test_recurring_jump_link_absent_with_no_recurring_transactions(auth_client, user):
    body = auth_client.get("/finance").get_data(as_text=True)
    assert 'class="recurring-jump-link"' not in body


def test_recurring_jump_link_shows_count(auth_client, user):
    _make_recurring(user, "Netflix", "15.99", category="subscriptions")
    _make_recurring(user, "Spotify", "10.99", category="subscriptions")
    body = auth_client.get("/finance").get_data(as_text=True)
    assert 'class="recurring-jump-link"' in body
    assert "2 recurring transactions set" in body


# --- /finance/breakdown?view=recurring ---------------------------------------

def test_recurring_breakdown_lists_bill_with_amount_and_category(auth_client, user):
    _make_recurring(user, "Netflix", "15.99", category="subscriptions")
    data = _recurring(auth_client)
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["description"] == "Netflix"
    assert item["amount"] == 15.99
    assert item["category"] == "subscriptions"


def test_recurring_breakdown_shows_income_bills_too(auth_client, user):
    _make_recurring(user, "Freelance retainer", "500.00", ttype="income", category="income")
    data = _recurring(auth_client)
    assert data["income_total"] == 500.00
    assert data["items"][0]["type"] == "income"


def test_recurring_breakdown_sorts_soonest_due_first(auth_client, user):
    # Already passed this month -> rolls to next month's occurrence.
    _make_recurring(user, "Later Bill", "10.00", day_offset=-1)
    # Still upcoming this month -> due sooner than "Later Bill" above.
    _make_recurring(user, "Sooner Bill", "20.00", day_offset=1)

    data = _recurring(auth_client)
    descriptions = [item["description"] for item in data["items"]]
    assert descriptions.index("Sooner Bill") < descriptions.index("Later Bill")


def test_recurring_breakdown_flags_bill_due_within_a_week(auth_client, user):
    _make_recurring(user, "Due Today", "9.99", day_offset=0)
    data = _recurring(auth_client)
    item = data["items"][0]
    assert item["due_soon"] is True
    assert item["due_label"] == "Today"


def test_recurring_breakdown_uses_most_recent_row_per_rule(auth_client, user):
    """Same recurring rule accumulates one row per month (generate_recurring)
    — the breakdown should use the latest amount/category, not an older
    one, same "most recent wins" dedup already used for recurring_count."""
    older = _make_recurring(user, "Gym", "40.00", category="health", day_offset=-40)
    older.timestamp = older.timestamp.replace(year=older.timestamp.year - 1)
    db.session.commit()
    _make_recurring(user, "Gym", "45.00", category="health", day_offset=0)

    data = _recurring(auth_client)
    assert len(data["items"]) == 1
    assert data["items"][0]["amount"] == 45.00


# --- Period scaling: the same monthly commitment projected over a window ----

def test_recurring_breakdown_scales_total_by_period_length(auth_client, user):
    _make_recurring(user, "Netflix", "15.99", ttype="expense")
    _make_recurring(user, "Paycheck", "3000.00", ttype="income")

    month_data = _recurring(auth_client, period="month")
    assert month_data["months"] == 1
    assert month_data["expense_total"] == 15.99
    assert month_data["income_total"] == 3000.00

    three_month_data = _recurring(auth_client, period="3m")
    assert three_month_data["months"] == 3
    assert three_month_data["expense_total"] == round(15.99 * 3, 2)
    assert three_month_data["income_total"] == 9000.00


def test_recurring_breakdown_year_to_date_scales_by_elapsed_months(auth_client, user):
    _make_recurring(user, "Netflix", "10.00", ttype="expense")
    today = date.today()

    data = _recurring(auth_client, period="year", year=today.year)
    assert data["months"] == today.month
    assert data["expense_total"] == round(10.00 * today.month, 2)


def test_recurring_breakdown_past_year_scales_by_full_12_months(auth_client, user):
    _make_recurring(user, "Netflix", "10.00", ttype="expense")
    data = _recurring(auth_client, period="year", year=2020)
    assert data["months"] == 12
    assert data["expense_total"] == 120.00
