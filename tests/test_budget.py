from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from artha.extensions import db
from artha.models import Transaction
from artha.models.budget import Budget
from artha.utils import budget_status

from .conftest import current_period_timestamp


def _add_expense(user, amount):
    tx = Transaction(
        description="Spend",
        amount=Decimal(amount),
        type="expense",
        user_id=user.id,
        timestamp=current_period_timestamp(),
    )
    db.session.add(tx)
    db.session.commit()


def test_set_budget_creates_row(auth_client, user):
    resp = auth_client.post("/finance/budget", data={"monthly_cap": "2000"}, follow_redirects=True)
    assert resp.status_code == 200
    row = Budget.query.filter_by(user_id=user.id).first()
    assert row is not None
    assert row.monthly_cap == Decimal("2000")


def test_set_budget_updates_existing_row_not_duplicate(auth_client, user):
    auth_client.post("/finance/budget", data={"monthly_cap": "2000"})
    auth_client.post("/finance/budget", data={"monthly_cap": "3000"})

    rows = Budget.query.filter_by(user_id=user.id).all()
    assert len(rows) == 1
    assert rows[0].monthly_cap == Decimal("3000")


def test_clearing_budget_sets_cap_to_zero(auth_client, user):
    auth_client.post("/finance/budget", data={"monthly_cap": "2000"})
    auth_client.post("/finance/budget", data={"monthly_cap": ""})

    row = Budget.query.filter_by(user_id=user.id).first()
    assert row.monthly_cap == Decimal("0")


def test_budget_requires_login(client):
    resp = client.post("/finance/budget", data={"monthly_cap": "2000"}, follow_redirects=False)
    assert resp.status_code in (302, 401)


def test_set_budget_ajax_returns_json_on_success(auth_client, user):
    resp = auth_client.post(
        "/finance/budget",
        data={"monthly_cap": "2000"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["message"]
    assert Budget.query.filter_by(user_id=user.id).first().monthly_cap == Decimal("2000")


def test_set_budget_ajax_returns_json_error_on_invalid_amount(auth_client, user):
    resp = auth_client.post(
        "/finance/budget",
        data={"monthly_cap": "not-a-number"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["message"]
    assert Budget.query.filter_by(user_id=user.id).first() is None


def test_finance_page_shows_warning_tier_at_90_percent(auth_client, user):
    _add_expense(user, "1800")
    auth_client.post("/finance/budget", data={"monthly_cap": "2000"})

    resp = auth_client.get("/finance")
    assert b"getting close" in resp.data


def test_finance_page_shows_over_tier_past_cap(auth_client, user):
    _add_expense(user, "2500")
    auth_client.post("/finance/budget", data={"monthly_cap": "2000"})

    resp = auth_client.get("/finance")
    assert b"over budget" in resp.data


def test_dashboard_banner_hidden_when_under_threshold(auth_client, user):
    _add_expense(user, "100")
    auth_client.post("/finance/budget", data={"monthly_cap": "2000"})

    resp = auth_client.get("/")
    assert b"monthly budget" not in resp.data


def test_dashboard_banner_shown_when_over_cap(auth_client, user):
    _add_expense(user, "2500")
    auth_client.post("/finance/budget", data={"monthly_cap": "2000"})

    body = auth_client.get("/").get_data(as_text=True)
    # The dollar figures are wrapped in spans (client-side reformatted to
    # the user's chosen currency) rather than plain inline text — assert
    # on the surrounding copy and the value separately instead of one
    # exact substring.
    assert "over your" in body
    assert "monthly budget" in body
    assert 'data-money-value="2000.0"' in body


def test_dashboard_matches_finance_tier_at_exact_boundary(auth_client, user):
    """Regression guard: the dashboard used to cast expense through float()
    before re-wrapping it in Decimal() for the budget-tier check, so an
    exact 90.00% boundary could round down to 89.99999999999999 and show
    tier "ok" while /finance (which never leaves Decimal) correctly showed
    "warning" for the identical numbers."""
    _add_expense(user, "642.06")
    auth_client.post("/finance/budget", data={"monthly_cap": "713.40"})

    finance_resp = auth_client.get("/finance")
    dashboard_resp = auth_client.get("/")

    assert b"getting close" in finance_resp.data
    assert b"spent 90% of your" in dashboard_resp.data


# --- Pure unit tests for the shared threshold logic itself ---

def test_budget_status_no_cap_set():
    result = budget_status(None, Decimal("500"))
    assert result == {"has_budget": False}


def test_budget_status_ok_tier():
    result = budget_status(Decimal("1000"), Decimal("500"))
    assert result["tier"] == "ok"


def test_budget_status_warning_tier_at_boundary():
    result = budget_status(Decimal("1000"), Decimal("900"))
    assert result["tier"] == "warning"


def test_budget_status_over_tier_at_boundary():
    result = budget_status(Decimal("1000"), Decimal("1000"))
    assert result["tier"] == "over"


# --- "Safe to spend" (dashboard) ---


def test_dashboard_safe_to_spend_absent_without_a_budget(auth_client, user):
    _add_expense(user, "100")
    body = auth_client.get("/").get_data(as_text=True)
    # ">Safe to Spend<", not the bare phrase -- the template's own HTML
    # comment above the card mentions "Safe to Spend" too, and that
    # comment renders regardless of whether the card itself does.
    assert ">Safe to Spend<" not in body


def test_dashboard_safe_to_spend_is_cap_minus_spent_with_no_upcoming_bills(auth_client, user):
    _add_expense(user, "500")
    auth_client.post("/finance/budget", data={"monthly_cap": "2000"})

    body = auth_client.get("/").get_data(as_text=True)
    assert ">Safe to Spend<" in body
    assert 'data-money-value="1500.0"' in body


def test_dashboard_safe_to_spend_nets_out_an_upcoming_recurring_bill(auth_client, user):
    today = date.today()
    # A day-of-month that (a) exists in every month, including February,
    # and (b) is still ahead of today -- lets the "template" transaction
    # below live in a genuinely different month (so it doesn't also
    # count toward this month's "spent") while next_due_date() still
    # resolves it to later *this* month, not skipped as flaky near
    # month-end instead of using a frozen clock for one date-relative case.
    target_day = today.day + 1
    if target_day > 28:
        pytest.skip("Needs a day-of-month that exists in every month and is still ahead of today.")

    _add_expense(user, "500")
    auth_client.post("/finance/budget", data={"monthly_cap": "2000"})

    prev_month = today.month - 1 or 12
    prev_year = today.year if today.month > 1 else today.year - 1
    upcoming_template = Transaction(
        description="Upcoming Rent",
        amount=Decimal("300"),
        type="expense",
        user_id=user.id,
        timestamp=datetime(prev_year, prev_month, target_day, 12, 0, tzinfo=timezone.utc),
        is_recurring=True,
    )
    db.session.add(upcoming_template)
    db.session.commit()

    body = auth_client.get("/").get_data(as_text=True)
    # 2000 cap - 500 already spent - 300 upcoming = 1200
    assert 'data-money-value="1200.0"' in body
    assert "in upcoming bills" in body


def test_dashboard_safe_to_spend_ignores_a_bill_already_posted_this_month(auth_client, user):
    # A recurring bill whose latest occurrence already landed earlier
    # this month must not be double-counted -- it's already inside
    # "spent", and next_due_date() from today should resolve to *next*
    # month for it, not this one.
    today = date.today()
    if today.day < 2:
        pytest.skip("Needs at least one earlier day this month to date the already-posted bill on.")

    _add_expense(user, "500")
    auth_client.post("/finance/budget", data={"monthly_cap": "2000"})

    already_posted = Transaction(
        description="Rent",
        amount=Decimal("300"),
        type="expense",
        user_id=user.id,
        timestamp=datetime(today.year, today.month, today.day - 1, 12, 0, tzinfo=timezone.utc),
        is_recurring=True,
    )
    db.session.add(already_posted)
    db.session.commit()

    body = auth_client.get("/").get_data(as_text=True)
    # 2000 cap - (500 + 300 already spent) - 0 upcoming = 1200, not 900.
    assert 'data-money-value="1200.0"' in body
