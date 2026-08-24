from datetime import date, datetime, timezone
from decimal import Decimal

from artha.extensions import db
from artha.models import Transaction


def _add_tx(user, description, amount, ttype, category=None, when=None):
    tx = Transaction(
        description=description,
        amount=Decimal(amount),
        type=ttype,
        category=category,
        user_id=user.id,
        timestamp=when or datetime.now(timezone.utc),
    )
    db.session.add(tx)
    db.session.commit()
    return tx


def _this_month(day=10):
    today = date.today()
    return datetime(today.year, today.month, day, 12, 0, tzinfo=timezone.utc)


def test_spending_breakdown_groups_by_category(auth_client, user):
    _add_tx(user, "Rent", "1500.00", "expense", category="housing", when=_this_month())
    _add_tx(user, "Groceries", "200.00", "expense", category="groceries", when=_this_month())
    _add_tx(user, "Paycheck", "3000.00", "income", category="income", when=_this_month())

    resp = auth_client.get("/finance/breakdown?view=spending&period=month")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total"] == 1700.00
    labels = {c["label"]: c["amount"] for c in data["categories"]}
    assert labels == {"Housing": 1500.00, "Groceries": 200.00}


def test_spending_breakdown_buckets_uncategorized_separately_from_other(auth_client, user):
    _add_tx(user, "Mystery charge", "40.00", "expense", category=None, when=_this_month())
    _add_tx(user, "Misc", "25.00", "expense", category="other", when=_this_month())

    resp = auth_client.get("/finance/breakdown?view=spending&period=month")
    data = resp.get_json()
    labels = {c["label"]: c["amount"] for c in data["categories"]}
    assert labels == {"Uncategorized": 40.00, "Other": 25.00}


def test_income_breakdown_only_includes_income_transactions(auth_client, user):
    _add_tx(user, "Paycheck", "2000.00", "income", category="income", when=_this_month())
    _add_tx(user, "Rent", "1000.00", "expense", category="housing", when=_this_month())

    resp = auth_client.get("/finance/breakdown?view=income&period=month")
    data = resp.get_json()
    assert data["total"] == 2000.00
    assert len(data["categories"]) == 1
    assert data["categories"][0]["label"] == "Income"


def test_cashflow_breakdown_buckets_by_month_within_period(auth_client, user):
    today = date.today()
    prev_month = today.month - 1 or 12
    prev_year = today.year if today.month > 1 else today.year - 1
    _add_tx(user, "Paycheck", "1000.00", "income", when=_this_month())
    _add_tx(user, "Paycheck", "900.00", "income", when=datetime(prev_year, prev_month, 10, tzinfo=timezone.utc))
    _add_tx(user, "Rent", "500.00", "expense", when=_this_month())

    resp = auth_client.get("/finance/breakdown?view=cashflow&period=3m")
    data = resp.get_json()
    assert data["income_total"] == 1900.00
    assert data["expense_total"] == 500.00
    assert data["net"] == 1400.00
    assert len(data["buckets"]) == 3


def test_cashflow_breakdown_year_period_uses_year_param_not_today(auth_client, user):
    _add_tx(user, "Paycheck", "5000.00", "income", when=datetime(2025, 6, 15, tzinfo=timezone.utc))
    _add_tx(user, "Paycheck", "9999.00", "income", when=_this_month())  # this year — must not leak in

    resp = auth_client.get("/finance/breakdown?view=cashflow&period=year&year=2025")
    data = resp.get_json()
    assert data["income_total"] == 5000.00
    assert data["period_label"] == "2025"
    assert len(data["buckets"]) == 12


def test_breakdown_defaults_to_spending_month_on_bad_params(auth_client, user):
    _add_tx(user, "Rent", "300.00", "expense", category="housing", when=_this_month())
    resp = auth_client.get("/finance/breakdown?view=nonsense&period=nonsense")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["view"] == "spending"
    assert data["period"] == "month"
    assert data["total"] == 300.00


def test_breakdown_requires_login(client):
    resp = client.get("/finance/breakdown?view=spending&period=month")
    assert resp.status_code in (302, 401)
