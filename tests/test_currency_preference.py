def test_set_currency_updates_preference(auth_client, user):
    resp = auth_client.post("/set_currency", json={"code": "gbp"})
    assert resp.status_code == 200
    assert user.preferred_currency == "GBP"


def test_set_currency_rejects_unknown_code(auth_client, user):
    resp = auth_client.post("/set_currency", json={"code": "XYZ"})
    assert resp.status_code == 400
    assert user.preferred_currency is None


def test_set_currency_invalid_code_leaves_existing_value_untouched(auth_client, user):
    auth_client.post("/set_currency", json={"code": "EUR"})
    auth_client.post("/set_currency", json={"code": "not-a-code"})
    assert user.preferred_currency == "EUR"


def test_set_currency_requires_login(client):
    resp = client.post("/set_currency", json={"code": "USD"}, follow_redirects=False)
    assert resp.status_code in (302, 401)


def test_new_user_has_no_currency_preference_by_default(user):
    assert user.preferred_currency is None


# base.html renders the account's effective display currency onto <body>,
# and static/js/currency.js treats it as the source of truth over any value
# the browser has in localStorage. It has to use the exact same
# `preferred_currency or "USD"` fallback the routes use when converting
# aggregates, or the page would format USD figures under another symbol.
def test_page_renders_usd_display_currency_when_account_has_none(auth_client, user):
    assert user.preferred_currency is None
    html = auth_client.get("/").get_data(as_text=True)
    assert 'data-display-currency="USD"' in html


def test_page_renders_account_display_currency(auth_client, user):
    auth_client.post("/set_currency", json={"code": "GBP"})
    html = auth_client.get("/").get_data(as_text=True)
    assert 'data-display-currency="GBP"' in html


def test_logged_out_page_has_no_display_currency(client):
    html = client.get("/login").get_data(as_text=True)
    assert "data-display-currency" not in html


# Any currency the exchange-rate provider covers, not just the original six
# (added 2026-10-22, starting from a request for the Hong Kong dollar).
def test_set_currency_accepts_any_supported_currency(auth_client, user):
    resp = auth_client.post("/set_currency", json={"code": "hkd"})
    assert resp.status_code == 200
    assert user.preferred_currency == "HKD"


def test_settings_offers_every_currency_with_the_common_six_first(auth_client, user):
    from artha.utils import CURRENCY_CODES, currency_choices
    assert {"HKD", "JPY", "INR", "CHF", "SGD"} <= CURRENCY_CODES
    assert len(CURRENCY_CODES) > 150
    assert [code for code, _ in currency_choices()[:6]] == ["USD", "EUR", "GBP", "CAD", "AUD", "BDT"]
    html = auth_client.get("/settings").get_data(as_text=True)
    assert '<option value="HKD">Hong Kong dollar (HKD)</option>' in html
    assert html.index('label="Common"') < html.index('label="All currencies"')


def test_currency_changed_on_one_device_shows_on_the_others(auth_client, user, app):
    # The currency lives on the account, so a change on the web is what the
    # app's API reports, and a change in the app is what the web renders.
    import json
    from artha.extensions import db
    from artha.models import ExchangeRate
    db.session.add(ExchangeRate(base="USD", source="open-er-api",
                                rates_json=json.dumps({"USD": 1, "HKD": 7.8, "JPY": 150.0})))
    db.session.commit()
    phone = app.test_client()
    token = phone.post("/api/v1/sessions", json={"username": user.username, "password": "password123"}).get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    auth_client.post("/set_currency", json={"code": "HKD"})
    assert phone.get("/api/v1/today", headers=headers).get_json()["currency"] == "HKD"
    assert phone.get("/api/v1/me", headers=headers).get_json()["user"]["currency"] == "HKD"

    resp = phone.patch("/api/v1/me", headers=headers, json={"currency": "JPY"})
    assert resp.status_code == 200, resp.get_json()
    assert 'data-display-currency="JPY"' in auth_client.get("/").get_data(as_text=True)


def test_format_money_handles_currencies_beyond_the_original_six():
    from artha.utils import format_money
    assert format_money("1234.5", "HKD") == "HK$1,234.50"
    assert format_money("1234.5", "JPY") == "¥1,235"
    assert format_money("-12", "CHF") == "-CHF 12.00"
    assert format_money("1234.5", "USD") == "$1,234.50"
    assert format_money("1234.5", "EUR") == "1.234,50 €"
