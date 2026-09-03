"""A transaction's own currency: locking a native amount to USD at
creation/import time, converting back to a live display currency for
totals/comparisons, and never mixing currencies in an aggregate sum.

Seeds a real ExchangeRate row (rather than mocking get_rates() itself,
which different modules each import their own reference to) so every
call site under test -- finance/dashboard/scenarios routes,
exchange_rate_service directly -- exercises the real DB-cached-rate code
path, exactly like production."""

import json
from decimal import Decimal
from unittest.mock import patch

import requests

from artha.extensions import db
from artha.models import ExchangeRate, Transaction
from artha.services.exchange_rate_service import lock_usd_value, convert_usd_to

from .conftest import current_period_timestamp

AJAX_HEADERS = {"X-Requested-With": "XMLHttpRequest"}

# 1 USD = these many units of each currency -- matches the shape
# open.er-api.com actually returns (base=USD).
RATES = {"USD": 1, "GBP": 0.80, "EUR": 0.92, "BDT": 110.0, "CAD": 1.35, "AUD": 1.50}


def _seed_rates(app):
    with app.app_context():
        row = ExchangeRate(base="USD", rates_json=json.dumps(RATES), source="open-er-api")
        db.session.add(row)
        db.session.commit()


def _add_tx(user, description, amount, ttype, currency="USD", when=None):
    amount = Decimal(amount)
    usd_value, rate_locked = lock_usd_value(amount, currency)
    tx = Transaction(
        description=description, amount=amount, type=ttype, user_id=user.id,
        timestamp=when or current_period_timestamp(),
        currency=currency, usd_value=usd_value, usd_rate_locked=rate_locked,
    )
    db.session.add(tx)
    db.session.commit()
    return tx


# ---------------------------------------------------------------------------
# lock_usd_value / convert_usd_to
# ---------------------------------------------------------------------------

def test_lock_usd_value_usd_short_circuits_with_no_rate_lookup(app):
    # No ExchangeRate row seeded at all -- if this needed a rate table it
    # would return (None, None); getting a real answer proves the USD
    # short-circuit skips the lookup entirely.
    with app.app_context():
        usd_value, rate = lock_usd_value(Decimal("42.50"), "USD")
    assert usd_value == Decimal("42.50")
    assert rate == Decimal("1")


def test_lock_usd_value_converts_non_usd_using_the_rate_table(app):
    _seed_rates(app)
    with app.app_context():
        usd_value, rate = lock_usd_value(Decimal("80.00"), "GBP")
    # 80 GBP / 0.80 (GBP per USD) = 100 USD
    assert usd_value == Decimal("100.00")
    assert rate == Decimal("0.8")


def test_lock_usd_value_returns_none_none_when_rates_unavailable(app):
    # No ExchangeRate row to fall back to, and the live provider forced
    # to fail -- deterministic regardless of this environment's actual
    # network access (which real CI/dev machines often do have).
    with app.app_context(), patch("requests.get", side_effect=requests.RequestException("down")):
        usd_value, rate = lock_usd_value(Decimal("50"), "GBP")
    assert usd_value is None
    assert rate is None


def test_convert_usd_to_usd_is_identity(app):
    with app.app_context():
        assert convert_usd_to(Decimal("77.00"), "USD") == Decimal("77.00")


def test_convert_usd_to_converts_using_a_live_rate(app):
    _seed_rates(app)
    with app.app_context():
        result = convert_usd_to(Decimal("100"), "EUR")
    assert result == Decimal("92.00")


def test_convert_usd_to_falls_back_to_usd_figure_when_unavailable(app):
    with app.app_context(), patch("requests.get", side_effect=requests.RequestException("down")):
        result = convert_usd_to(Decimal("50"), "GBP")
    assert result == Decimal("50")


def test_convert_amount_same_currency_is_identity_no_rate_lookup(app):
    from artha.services.exchange_rate_service import convert_amount
    # No ExchangeRate row seeded -- a rate lookup would return None,None
    # via the unreachable-provider path; getting the exact answer back
    # proves the same-currency case skips it entirely.
    with app.app_context():
        assert convert_amount(Decimal("42"), "GBP", "GBP") == Decimal("42")


def test_convert_amount_converts_between_two_non_usd_currencies(app):
    from artha.services.exchange_rate_service import convert_amount
    _seed_rates(app)
    with app.app_context():
        # 100 GBP -> USD (100/0.80=125) -> BDT (125*110=13750)
        result = convert_amount(Decimal("100"), "GBP", "BDT")
    assert result == Decimal("13750")


# ---------------------------------------------------------------------------
# Transaction.native_currency / value_in_usd
# ---------------------------------------------------------------------------

def test_legacy_row_with_no_currency_reads_as_usd(app, user):
    tx = Transaction(
        description="Predates this column", amount=Decimal("25.00"),
        type="expense", user_id=user.id, timestamp=current_period_timestamp(),
    )
    db.session.add(tx)
    db.session.commit()
    assert tx.native_currency == "USD"
    assert tx.value_in_usd == Decimal("25.00")


def test_locked_row_reports_its_own_currency_and_usd_value(app, user):
    _seed_rates(app)
    tx = _add_tx(user, "Pub", "20.00", "expense", currency="GBP")
    assert tx.native_currency == "GBP"
    assert tx.value_in_usd == Decimal("25.00")  # 20 / 0.80


# ---------------------------------------------------------------------------
# Currency capture at creation
# ---------------------------------------------------------------------------

def test_add_transaction_captures_the_active_display_currency(app, auth_client, user):
    _seed_rates(app)
    resp = auth_client.post("/add_transaction", data={
        "description": "Souvenir", "amount": "15.00", "type": "expense", "currency": "EUR",
    }, headers=AJAX_HEADERS)
    assert resp.status_code == 200
    tx = Transaction.query.filter_by(description="Souvenir").first()
    assert tx is not None
    assert tx.currency == "EUR"
    assert tx.usd_value == Decimal("16.30")  # 15 / 0.92, rounded


def test_add_transaction_defaults_to_usd_when_currency_omitted(app, auth_client, user):
    resp = auth_client.post("/add_transaction", data={
        "description": "Coffee", "amount": "5.00", "type": "expense",
    }, headers=AJAX_HEADERS)
    assert resp.status_code == 200
    tx = Transaction.query.filter_by(description="Coffee").first()
    assert tx.currency == "USD"
    assert tx.usd_value == Decimal("5.00")


def test_add_transaction_rejects_unrecognized_currency_by_falling_back(app, auth_client, user):
    resp = auth_client.post("/add_transaction", data={
        "description": "Weird", "amount": "5.00", "type": "expense", "currency": "XYZ",
    }, headers=AJAX_HEADERS)
    assert resp.status_code == 200
    tx = Transaction.query.filter_by(description="Weird").first()
    assert tx.currency == "USD"


# ---------------------------------------------------------------------------
# Aggregation correctness -- the core bug this feature fixed
# ---------------------------------------------------------------------------

def test_finance_totals_sums_mixed_currency_transactions_correctly(app, auth_client, user):
    _seed_rates(app)
    _add_tx(user, "USD income", "100.00", "income", currency="USD")
    _add_tx(user, "GBP income", "80.00", "income", currency="GBP")  # = $100

    resp = auth_client.get("/api/finance_totals")
    assert resp.status_code == 200
    data = resp.get_json()
    # user.preferred_currency is unset -> USD: 100 + 100(from 80 GBP) = 200,
    # not a nonsensical raw sum of "100 + 80 = 180".
    assert data["income"] == 200.0


def test_finance_totals_converts_to_the_users_preferred_currency(app, auth_client, user):
    _seed_rates(app)
    user.preferred_currency = "EUR"
    db.session.commit()
    _add_tx(user, "USD expense", "92.00", "expense", currency="USD")  # = €84.64

    resp = auth_client.get("/api/finance_totals")
    data = resp.get_json()
    assert data["expense"] == 84.64


def test_dashboard_income_expense_are_currency_correct(app, auth_client, user):
    _seed_rates(app)
    _add_tx(user, "USD income", "50.00", "income", currency="USD")
    _add_tx(user, "GBP income", "40.00", "income", currency="GBP")  # = $50

    resp = auth_client.get("/")
    assert resp.status_code == 200
    assert "$100.00" in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Budget comparisons -- Budget.monthly_cap carries no currency of its own
# ---------------------------------------------------------------------------

def test_budget_status_compares_in_the_users_own_currency(app, auth_client, user):
    from artha.models.budget import Budget

    _seed_rates(app)
    user.preferred_currency = "GBP"
    db.session.commit()
    db.session.add(Budget(user_id=user.id, monthly_cap=Decimal("100"), currency="GBP"))
    # A $115 USD expense converts to £92 (115 * 0.80) -- 92% of a £100
    # cap, "warning" tier. Comparing the raw USD-pivot figure ($115)
    # against a cap meant as £100 would instead read 115%, "over" tier --
    # the wrong banner and the wrong percentage text.
    _add_tx(user, "Big purchase", "115.00", "expense", currency="USD")
    db.session.commit()

    resp = auth_client.get("/")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "You've spent 92% of your" in body


def test_budget_cap_stays_correct_after_switching_display_currency(app, auth_client, user):
    """The exact regression a real user hit live: a cap set while
    browsing in USD must still mean the same real amount after the user
    later switches their display currency -- not get silently reread as
    if it had always been in the new currency (a $5,000 cap misread as
    a BDT5,000 cap the moment someone switches to Taka)."""
    from artha.models.budget import Budget

    _seed_rates(app)
    # Cap set while preferred_currency was (implicitly) USD -- currency
    # left unset here on purpose, matching a pre-this-fix row exactly.
    db.session.add(Budget(user_id=user.id, monthly_cap=Decimal("5000")))
    _add_tx(user, "Groceries", "713.05", "expense", currency="USD")
    db.session.commit()

    # Now switch to BDT, matching set_currency()'s own persistence.
    user.preferred_currency = "BDT"
    db.session.commit()

    resp = auth_client.get("/")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    # $713.05 spent against a real $5,000 cap is 14%, nowhere near
    # "over" -- if the cap were misread as ৳5,000 instead of the
    # converted ~৳613,850 it actually is, this would wrongly say "over".
    assert "over your" not in body


def test_set_budget_captures_currency_active_at_save_time(app, auth_client, user):
    user.preferred_currency = "EUR"
    db.session.commit()

    resp = auth_client.post("/finance/budget", data={"monthly_cap": "500"}, headers=AJAX_HEADERS)
    assert resp.status_code == 200

    from artha.models.budget import Budget
    row = Budget.query.filter_by(user_id=user.id).first()
    assert row.currency == "EUR"
    assert row.monthly_cap == Decimal("500")


def test_scenario_comparison_converts_scenario_currency_to_display_currency(app, user):
    """The same class of bug as Budget.monthly_cap, in Scenario: costs
    typed in one currency combined with real transaction totals already
    converted to the user's display currency."""
    from artha.models.scenario import Scenario
    from artha.blueprints.scenarios.routes import _scenario_month_comparison

    _seed_rates(app)
    user.preferred_currency = "GBP"
    db.session.commit()

    # $100/mo cost, typed in USD -- converts to £80.
    scenario = Scenario(
        user_id=user.id, title="New gym", monthly_cost=Decimal("100"), currency="USD",
    )
    db.session.add(scenario)
    _add_tx(user, "Salary", "1000.00", "income", currency="USD")  # -> £800
    db.session.commit()

    with app.app_context():
        comparison = _scenario_month_comparison(scenario)

    # net = 800 (income) - 0 (no expenses); scenario effect = -80 (cost,
    # converted). If the £100... sorry $100 cost were left unconverted
    # and just subtracted as if it were already GBP, net_with_scenario
    # would be 800 - 100 = 700 instead of the correct 800 - 80 = 720.
    assert comparison["net_with_scenario"] == Decimal("720.00")


# ---------------------------------------------------------------------------
# Import currency detection
# ---------------------------------------------------------------------------

def test_detect_import_currency_finds_unambiguous_symbols():
    from artha.blueprints.finance.routes import _detect_import_currency

    assert _detect_import_currency("Statement total: £45.00", "USD") == "GBP"
    assert _detect_import_currency("Total: €12.50", "USD") == "EUR"
    assert _detect_import_currency("Total: ৳500.00", "USD") == "BDT"


def test_detect_import_currency_falls_back_for_bare_dollar_or_no_symbol():
    from artha.blueprints.finance.routes import _detect_import_currency

    # A bare "$" is deliberately not a signal (spans USD/CAD/AUD with no
    # dominant currency, same reasoning _detect_day_first already uses
    # for date order) -- falls back to the caller's own default instead.
    assert _detect_import_currency("Total: $45.00", "CAD") == "CAD"
    assert _detect_import_currency("no symbol at all here", "USD") == "USD"
    assert _detect_import_currency("no symbol at all here", "BDT") == "BDT"
