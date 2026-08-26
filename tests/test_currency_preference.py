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
