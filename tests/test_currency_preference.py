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
