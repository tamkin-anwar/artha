from datetime import datetime, timezone
from decimal import Decimal

from artha.extensions import db
from artha.models import Transaction
from artha.models.budget import Budget
from artha.utils import budget_status


def _add_expense(user, amount):
    tx = Transaction(
        description="Spend",
        amount=Decimal(amount),
        type="expense",
        user_id=user.id,
        timestamp=datetime.now(timezone.utc),
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
