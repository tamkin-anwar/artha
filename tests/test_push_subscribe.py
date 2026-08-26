from artha.models.push_subscription import PushSubscription


def test_subscribe_works_without_csrf_token_even_when_csrf_enabled(app, auth_client, user):
    """/push/subscribe is called from inside the service worker's
    pushsubscriptionchange handler, which has no DOM to read a CSRF meta
    tag from — it must stay exempt regardless of the app's CSRF setting."""
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        resp = auth_client.post(
            "/push/subscribe",
            json={"endpoint": "https://example.com/no-csrf", "keys": {"p256dh": "a", "auth": "b"}},
        )
        assert resp.status_code == 201
        assert PushSubscription.query.filter_by(endpoint="https://example.com/no-csrf").first() is not None
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


def test_subscribe_still_requires_login(client):
    resp = client.post(
        "/push/subscribe",
        json={"endpoint": "https://example.com/nologin", "keys": {"p256dh": "a", "auth": "b"}},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 401)
    assert PushSubscription.query.filter_by(endpoint="https://example.com/nologin").first() is None


def test_resubscribe_same_endpoint_upserts_not_duplicates(auth_client, user):
    payload = {"endpoint": "https://example.com/rotate", "keys": {"p256dh": "a", "auth": "b"}}
    auth_client.post("/push/subscribe", json=payload)
    auth_client.post("/push/subscribe", json=payload)

    rows = PushSubscription.query.filter_by(endpoint="https://example.com/rotate").all()
    assert len(rows) == 1


def test_notification_preferences_default_to_true(user):
    assert user.notify_bills_due is True
    assert user.notify_notes_due is True


def test_set_preferences_updates_both_flags(auth_client, user):
    resp = auth_client.post(
        "/push/preferences", json={"notify_bills_due": False, "notify_notes_due": False}
    )
    assert resp.status_code == 200
    assert user.notify_bills_due is False
    assert user.notify_notes_due is False


def test_set_preferences_partial_update_leaves_other_flag_untouched(auth_client, user):
    auth_client.post("/push/preferences", json={"notify_bills_due": False})
    assert user.notify_bills_due is False
    assert user.notify_notes_due is True

    auth_client.post("/push/preferences", json={"notify_notes_due": False})
    assert user.notify_bills_due is False
    assert user.notify_notes_due is False


def test_set_preferences_requires_login(client):
    resp = client.post("/push/preferences", json={"notify_bills_due": False}, follow_redirects=False)
    assert resp.status_code in (302, 401)
