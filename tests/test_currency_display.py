"""
Money that's already been converted server-side must reach the page as a
raw value with no hardcoded "$", and a single item's own native amount
must carry its own currency -- Calendar and Scenarios used to do neither.
"""

import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal

from artha.extensions import db
from artha.models import ExchangeRate, Transaction
from artha.models.scenario import Scenario
from artha.services.exchange_rate_service import lock_usd_value

RATES = {"USD": 1, "GBP": 0.80, "EUR": 0.92, "BDT": 110.0, "CAD": 1.35, "AUD": 1.50}


def _seed_rates():
    db.session.add(ExchangeRate(base="USD", rates_json=json.dumps(RATES), source="open-er-api"))
    db.session.commit()


def _calendar_data(body: str) -> dict:
    match = re.search(r"window\.CALENDAR_DATA = (\{.*?\});\n", body, re.S)
    return json.loads(match.group(1))


def test_calendar_rows_carry_their_own_currency_and_a_display_value(auth_client, user):
    _seed_rates()
    today = date.today()
    usd_value, rate = lock_usd_value(Decimal("20.00"), "GBP")
    db.session.add(Transaction(
        description="UK subscription", amount=Decimal("20.00"), type="expense", user_id=user.id,
        is_recurring=True, currency="GBP", usd_value=usd_value, usd_rate_locked=rate,
        timestamp=datetime(today.year, today.month, today.day, 12, 0, tzinfo=timezone.utc),
    ))
    db.session.commit()

    body = auth_client.get("/calendar").get_data(as_text=True)
    row = _calendar_data(body)[today.strftime("%Y-%m-%d")][0]

    assert row["amount"] == 20.0
    assert row["currency"] == "GBP"
    assert row["display_amount"] == 25.0  # 20 GBP -> $25, the user's display currency


def test_calendar_day_net_chip_uses_the_display_currency_symbol(auth_client, user):
    _seed_rates()
    user.preferred_currency = "GBP"
    db.session.commit()
    today = date.today()
    db.session.add(Transaction(
        description="Salary", amount=Decimal("100.00"), type="income", user_id=user.id,
        currency="USD", usd_value=Decimal("100.00"),
        timestamp=datetime(today.year, today.month, today.day, 12, 0, tzinfo=timezone.utc),
    ))
    db.session.commit()

    body = auth_client.get("/calendar").get_data(as_text=True)

    assert 'data-cal-net="80.0"' in body
    assert "+£80" in body
    assert "+$80" not in body


def test_scenario_figures_carry_the_scenarios_own_currency(auth_client, user):
    scenario = Scenario(user_id=user.id, title="Gym", monthly_cost=Decimal("40"), currency="GBP")
    db.session.add(scenario)
    db.session.commit()

    body = auth_client.get(f"/scenarios/{scenario.id}").get_data(as_text=True)

    assert 'data-money-value="40.00" data-money-currency="GBP" data-money-suffix="/mo"' in body
    assert "$40.00/mo" not in body
