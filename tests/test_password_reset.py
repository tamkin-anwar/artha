from unittest.mock import patch
from urllib.parse import urlparse

import pytest

from artha.blueprints.auth import routes as auth_routes
from artha.extensions import db as _db
from artha.models import User

from .conftest import make_user


def _captured_token(mock_send) -> str:
    """Pulls the reset token out of the reset_url passed to the (mocked)
    email sender — mirrors how a real user would get it, just via the
    mock's call args instead of an inbox."""
    assert mock_send.called
    _user, reset_url = mock_send.call_args[0]
    path = urlparse(reset_url).path
    return path.rsplit("/", 1)[-1]


@patch("artha.blueprints.auth.routes.send_password_reset_email")
def test_forgot_password_same_message_for_existing_and_unknown_email(mock_send, client, user):
    resp_known = client.post("/forgot_password", data={"email": user.email}, follow_redirects=True)
    resp_unknown = client.post(
        "/forgot_password", data={"email": "nobody@example.com"}, follow_redirects=True
    )

    assert b"a reset link is on its way" in resp_known.data
    assert b"a reset link is on its way" in resp_unknown.data
    # Only the real match should have triggered an actual send attempt.
    mock_send.assert_called_once()


@patch("artha.blueprints.auth.routes.send_password_reset_email")
def test_reset_password_with_valid_token_succeeds(mock_send, client, user):
    client.post("/forgot_password", data={"email": user.email})
    token = _captured_token(mock_send)

    resp = client.post(
        f"/reset_password/{token}",
        data={"new_password": "newpassword456", "confirm_password": "newpassword456"},
        follow_redirects=True,
    )
    assert b"Password reset" in resp.data

    refreshed = _db.session.get(User, user.id)
    assert refreshed.check_password("newpassword456")
    assert not refreshed.check_password("password123")


@patch("artha.blueprints.auth.routes.send_password_reset_email")
def test_reset_password_rejects_expired_token(mock_send, client, user, monkeypatch):
    # -1, not 0: itsdangerous timestamps a token to the second, so a token
    # verified in the same second it was issued has age 0 — max_age=0
    # would not (yet) count that as expired. -1 forces expiry
    # unconditionally regardless of timing.
    monkeypatch.setattr(auth_routes, "RESET_TOKEN_MAX_AGE", -1)
    client.post("/forgot_password", data={"email": user.email})
    token = _captured_token(mock_send)

    resp = client.get(f"/reset_password/{token}", follow_redirects=True)
    assert b"invalid or has expired" in resp.data


@patch("artha.blueprints.auth.routes.send_password_reset_email")
def test_reset_link_becomes_invalid_after_password_already_changed(mock_send, client, user):
    client.post("/forgot_password", data={"email": user.email})
    token = _captured_token(mock_send)

    # A second, independent password change (e.g. the user reset it once
    # already, or changed it from Settings) should invalidate the first
    # link — it's still "unused" in the sense that reset_password was
    # never called with it, but the password it was issued for is gone.
    user.set_password("somethingelseentirely")
    _db.session.commit()

    resp = client.get(f"/reset_password/{token}", follow_redirects=True)
    assert b"invalid or has expired" in resp.data


@patch("artha.blueprints.auth.routes.send_password_reset_email")
def test_reset_password_enforces_minimum_length(mock_send, client, user):
    client.post("/forgot_password", data={"email": user.email})
    token = _captured_token(mock_send)

    resp = client.post(
        f"/reset_password/{token}",
        data={"new_password": "short", "confirm_password": "short"},
        follow_redirects=True,
    )
    assert b"at least 8 characters" in resp.data

    refreshed = _db.session.get(User, user.id)
    assert refreshed.check_password("password123")


@patch("artha.blueprints.auth.routes.send_password_reset_email")
def test_reset_password_requires_matching_confirmation(mock_send, client, user):
    client.post("/forgot_password", data={"email": user.email})
    token = _captured_token(mock_send)

    resp = client.post(
        f"/reset_password/{token}",
        data={"new_password": "newpassword456", "confirm_password": "somethingdifferent"},
        follow_redirects=True,
    )
    assert b"do not match" in resp.data


def test_forgot_password_is_rate_limited(rate_limited_client):
    make_user()
    for _ in range(3):
        rate_limited_client.post(
            "/forgot_password", data={"email": "nobody@example.com"}, follow_redirects=True
        )

    resp = rate_limited_client.post(
        "/forgot_password", data={"email": "nobody@example.com"}, follow_redirects=True
    )
    assert b"Too many attempts" in resp.data


def test_forgot_password_page_views_are_never_rate_limited(rate_limited_client):
    for _ in range(10):
        resp = rate_limited_client.get("/forgot_password")
        assert resp.status_code == 200
