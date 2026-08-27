"""Two-factor authentication: TOTP enrollment (with one-time recovery
codes as the lost-phone fallback) and the split login flow it adds when
enabled. See blueprints/auth/routes.py's enable_2fa()/disable_2fa()/
verify_2fa()/login() and _verify_totp_or_recovery_code().
"""

import pyotp
from werkzeug.security import generate_password_hash

from artha.extensions import db
from artha.models import User, RecoveryCode

from .conftest import make_user


def test_enable_2fa_get_shows_a_pending_secret(auth_client):
    resp = auth_client.get("/account/2fa/enable")
    assert resp.status_code == 200

    with auth_client.session_transaction() as sess:
        assert sess.get("pending_otp_secret")


def test_enable_2fa_wrong_code_does_not_activate(auth_client, user):
    auth_client.get("/account/2fa/enable")
    resp = auth_client.post(
        "/account/2fa/enable", data={"code": "000000"}, follow_redirects=True
    )
    assert b"code" in resp.data.lower()
    assert User.query.filter_by(username=user.username).first().otp_enabled is False


def test_enable_2fa_correct_code_activates_and_issues_recovery_codes(auth_client, user):
    auth_client.get("/account/2fa/enable")
    with auth_client.session_transaction() as sess:
        secret = sess["pending_otp_secret"]

    code = pyotp.TOTP(secret).now()
    resp = auth_client.post("/account/2fa/enable", data={"code": code}, follow_redirects=True)
    assert resp.status_code == 200

    refreshed = User.query.filter_by(username=user.username).first()
    assert refreshed.otp_enabled is True
    assert refreshed.otp_secret == secret
    assert refreshed.recovery_codes.count() == 10
    # Codes shown on the confirmation page itself, in the response body.
    assert b"saved these" in resp.data.lower() or b"recovery" in resp.data.lower()


def test_login_for_2fa_enabled_user_does_not_complete_without_the_second_step(client):
    secret = pyotp.random_base32()
    enrolled = make_user(username="has-2fa", password="password123", otp_enabled=True, otp_secret=secret)

    resp = client.post(
        "/login",
        data={"username": enrolled.username, "password": "password123"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/login/verify-2fa" in resp.headers["Location"]

    # Not actually logged in yet.
    dash_resp = client.get("/")
    assert dash_resp.status_code == 302
    assert "/login" in dash_resp.headers["Location"]


def test_verify_2fa_correct_totp_code_completes_login(client):
    secret = pyotp.random_base32()
    enrolled = make_user(username="has-2fa-2", password="password123", otp_enabled=True, otp_secret=secret)
    client.post("/login", data={"username": enrolled.username, "password": "password123"})

    resp = client.post(
        "/login/verify-2fa", data={"code": pyotp.TOTP(secret).now()}, follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"

    dash_resp = client.get("/")
    assert dash_resp.status_code == 200


def test_verify_2fa_wrong_code_is_rejected(client):
    secret = pyotp.random_base32()
    enrolled = make_user(username="has-2fa-3", password="password123", otp_enabled=True, otp_secret=secret)
    client.post("/login", data={"username": enrolled.username, "password": "password123"})

    resp = client.post("/login/verify-2fa", data={"code": "000000"}, follow_redirects=True)
    assert b"Invalid code" in resp.data

    dash_resp = client.get("/")
    assert dash_resp.status_code == 302  # still not logged in


def test_recovery_code_works_exactly_once(client):
    secret = pyotp.random_base32()
    enrolled = make_user(username="has-2fa-4", password="password123", otp_enabled=True, otp_secret=secret)
    raw_code = "abcd1234"
    db.session.add(RecoveryCode(user_id=enrolled.id, code_hash=generate_password_hash(raw_code)))
    db.session.commit()

    client.post("/login", data={"username": enrolled.username, "password": "password123"})
    resp = client.post("/login/verify-2fa", data={"code": raw_code}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"
    client.get("/logout")

    # Same code again: rejected, since it's already spent.
    client.post("/login", data={"username": enrolled.username, "password": "password123"})
    resp = client.post("/login/verify-2fa", data={"code": raw_code}, follow_redirects=True)
    assert b"Invalid code" in resp.data


def test_disable_2fa_requires_correct_password(auth_client, user):
    secret = pyotp.random_base32()
    user.otp_enabled = True
    user.otp_secret = secret
    db.session.commit()

    resp = auth_client.post(
        "/account/2fa/disable",
        data={"password": "wrong-password", "code": pyotp.TOTP(secret).now()},
        follow_redirects=True,
    )
    assert b"incorrect" in resp.data
    assert User.query.filter_by(username=user.username).first().otp_enabled is True


def test_disable_2fa_requires_valid_code(auth_client, user):
    secret = pyotp.random_base32()
    user.otp_enabled = True
    user.otp_secret = secret
    db.session.commit()

    resp = auth_client.post(
        "/account/2fa/disable",
        data={"password": "password123", "code": "000000"},
        follow_redirects=True,
    )
    assert b"Invalid code" in resp.data
    assert User.query.filter_by(username=user.username).first().otp_enabled is True


def test_disable_2fa_succeeds_and_clears_recovery_codes(auth_client, user):
    secret = pyotp.random_base32()
    user.otp_enabled = True
    user.otp_secret = secret
    db.session.add(RecoveryCode(user_id=user.id, code_hash=generate_password_hash("zzzz9999")))
    db.session.commit()

    resp = auth_client.post(
        "/account/2fa/disable",
        data={"password": "password123", "code": pyotp.TOTP(secret).now()},
        follow_redirects=True,
    )
    assert b"disabled" in resp.data.lower()

    refreshed = User.query.filter_by(username=user.username).first()
    assert refreshed.otp_enabled is False
    assert refreshed.otp_secret is None
    assert refreshed.recovery_codes.count() == 0
