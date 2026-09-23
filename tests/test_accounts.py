"""Accounts / net worth (assets minus liabilities, as of right now) --
deliberately a different figure from the existing Net Balance stat card
(income minus expenses, this month only). Manually-tracked balances only,
no live bank sync and no link to Transaction (see artha/models/account.py's
docstring for why) -- currency conversion follows the same live,
never-locked pattern as Budget.monthly_cap/Scenario's cost fields, since a
balance is an ongoing setting that gets re-typed, not a point-in-time
transaction.
"""

import json
from decimal import Decimal

from artha.extensions import db
from artha.models import ExchangeRate
from artha.models.account import ACCOUNT_TYPES, Account, NetWorthSnapshot

AJAX_HEADERS = {"X-Requested-With": "XMLHttpRequest"}

RATES = {"USD": 1, "GBP": 0.80, "EUR": 0.92, "BDT": 110.0, "CAD": 1.35, "AUD": 1.50}


def _seed_rates(app):
    with app.app_context():
        db.session.add(ExchangeRate(base="USD", rates_json=json.dumps(RATES), source="open-er-api"))
        db.session.commit()


def _add_account(user, name="Checking", account_type="checking", currency="USD", balance="100.00"):
    account = Account(
        user_id=user.id, name=name, account_type=account_type,
        currency=currency, current_balance=Decimal(balance),
    )
    db.session.add(account)
    db.session.commit()
    return account


# ---------------------------------------------------------------------------
# Model properties
# ---------------------------------------------------------------------------

def test_account_type_liability_flag_matches_account_types_table(app, user):
    checking = _add_account(user, account_type="checking")
    credit_card = _add_account(user, account_type="credit_card")
    assert checking.is_liability is False
    assert credit_card.is_liability is True
    assert checking.type_label == "Checking"
    assert credit_card.type_label == "Credit Card"


def test_every_account_type_is_flagged_as_asset_or_liability():
    for key, info in ACCOUNT_TYPES.items():
        assert isinstance(info["liability"], bool)
        assert info["label"]


# ---------------------------------------------------------------------------
# CRUD routes
# ---------------------------------------------------------------------------

def test_add_account_creates_a_row_and_redirects(app, auth_client, user):
    resp = auth_client.post("/accounts/add", data={
        "name": "Chase Checking", "account_type": "checking",
        "currency": "USD", "current_balance": "1234.56",
    })
    assert resp.status_code == 302

    account = Account.query.filter_by(user_id=user.id, name="Chase Checking").first()
    assert account is not None
    assert account.current_balance == Decimal("1234.56")
    assert account.currency == "USD"
    assert account.account_type == "checking"


def test_add_account_requires_a_name(app, auth_client, user):
    resp = auth_client.post("/accounts/add", data={
        "name": "", "account_type": "checking", "currency": "USD", "current_balance": "10",
    })
    assert resp.status_code == 302
    assert Account.query.filter_by(user_id=user.id).count() == 0


def test_add_account_rejects_negative_balance(app, auth_client, user):
    resp = auth_client.post("/accounts/add", data={
        "name": "Weird", "account_type": "checking", "currency": "USD", "current_balance": "-5",
    })
    assert resp.status_code == 302
    assert Account.query.filter_by(user_id=user.id, name="Weird").count() == 0


def test_add_account_rejects_unknown_type(app, auth_client, user):
    resp = auth_client.post("/accounts/add", data={
        "name": "Odd", "account_type": "crypto_wallet", "currency": "USD", "current_balance": "10",
    })
    assert resp.status_code == 302
    assert Account.query.filter_by(user_id=user.id, name="Odd").count() == 0


def test_edit_account_updates_balance_and_bumps_updated_at(app, auth_client, user):
    account = _add_account(user, balance="100.00")
    original_updated_at = account.balance_updated_at

    resp = auth_client.post(f"/accounts/{account.id}/edit", data={
        "name": account.name, "account_type": account.account_type,
        "currency": account.currency, "current_balance": "250.00",
    })
    assert resp.status_code == 302

    db.session.refresh(account)
    assert account.current_balance == Decimal("250.00")
    assert account.balance_updated_at >= original_updated_at


def test_edit_account_owned_by_someone_else_is_not_found(app, auth_client, user):
    from .conftest import make_user
    other = make_user(username="bob")
    other_account = _add_account(other, name="Bob's account")

    resp = auth_client.get(f"/accounts/{other_account.id}/edit")
    assert resp.status_code == 404


def test_delete_account_removes_the_row(app, auth_client, user):
    account = _add_account(user)
    resp = auth_client.post(f"/accounts/{account.id}/delete")
    assert resp.status_code == 302
    assert db.session.get(Account, account.id) is None


def test_delete_account_owned_by_someone_else_returns_404_json_for_ajax(app, auth_client, user):
    from .conftest import make_user
    other = make_user(username="carol")
    other_account = _add_account(other, name="Carol's account")

    resp = auth_client.post(f"/accounts/{other_account.id}/delete", headers=AJAX_HEADERS)
    assert resp.status_code == 404
    assert db.session.get(Account, other_account.id) is not None


# ---------------------------------------------------------------------------
# Net worth computation -- assets minus liabilities, multi-currency
# ---------------------------------------------------------------------------

def test_net_worth_sums_assets_minus_liabilities_same_currency(app, auth_client, user):
    _add_account(user, name="Checking", account_type="checking", balance="1000")
    _add_account(user, name="Credit Card", account_type="credit_card", balance="200")

    resp = auth_client.get("/accounts/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Net worth = 1000 - 200 = 800
    assert "$800.00" in body


def test_net_worth_converts_mixed_currency_accounts_correctly(app, auth_client, user):
    _seed_rates(app)
    # 80 GBP checking (-> $100) as an asset, no liabilities.
    _add_account(user, name="UK Checking", account_type="checking", currency="GBP", balance="80.00")

    resp = auth_client.get("/accounts/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "$100.00" in body


def test_net_worth_summary_is_none_with_no_accounts(app, user):
    from artha.blueprints.accounts.routes import net_worth_summary
    with app.app_context():
        assert net_worth_summary(user) is None


def test_net_worth_summary_converts_to_preferred_currency(app, user):
    from artha.blueprints.accounts.routes import net_worth_summary
    _seed_rates(app)
    user.preferred_currency = "EUR"
    db.session.commit()
    _add_account(user, currency="USD", balance="92.00")  # -> €84.64

    with app.app_context():
        summary = net_worth_summary(user)
    assert summary["net_worth"] == 84.64
    assert summary["account_count"] == 1


def test_dashboard_shows_net_worth_card_only_with_accounts(app, auth_client, user):
    # ">Net Worth<" (the rendered eyebrow span), not a bare substring --
    # the card is wrapped in an HTML comment that also contains the
    # literal phrase "Net Worth" and would always match otherwise (the
    # same class of false-positive test_budget.py's Safe to Spend test
    # hit and fixed).
    resp = auth_client.get("/")
    assert ">Net Worth<" not in resp.get_data(as_text=True)

    _add_account(user, balance="500")
    resp = auth_client.get("/")
    body = resp.get_data(as_text=True)
    assert ">Net Worth<" in body
    assert "$500.00" in body


# ---------------------------------------------------------------------------
# Net worth snapshot -- one row per user per day, upserted
# ---------------------------------------------------------------------------

def test_adding_an_account_creates_todays_snapshot(app, auth_client, user):
    auth_client.post("/accounts/add", data={
        "name": "Savings", "account_type": "savings", "currency": "USD", "current_balance": "300",
    })
    snapshots = NetWorthSnapshot.query.filter_by(user_id=user.id).all()
    assert len(snapshots) == 1
    assert snapshots[0].total_assets_usd == Decimal("300")
    assert snapshots[0].net_worth_usd == Decimal("300")


def test_editing_an_account_updates_todays_snapshot_in_place(app, auth_client, user):
    auth_client.post("/accounts/add", data={
        "name": "Savings", "account_type": "savings", "currency": "USD", "current_balance": "100",
    })
    assert NetWorthSnapshot.query.filter_by(user_id=user.id).count() == 1
    account = Account.query.filter_by(user_id=user.id, name="Savings").first()

    auth_client.post(f"/accounts/{account.id}/edit", data={
        "name": account.name, "account_type": account.account_type,
        "currency": account.currency, "current_balance": "900",
    })

    # Still exactly one row for today, not a second one -- the same-day
    # upsert, not an ever-growing history of every edit.
    snapshots = NetWorthSnapshot.query.filter_by(user_id=user.id).all()
    assert len(snapshots) == 1
    assert snapshots[0].total_assets_usd == Decimal("900")
