import pytest

from artha import create_app
from artha.config import TestingConfig
from artha.extensions import db as _db
from artha.extensions import limiter
from artha.models import User

from .conftest import make_user


@pytest.fixture()
def rate_limited_client(monkeypatch):
    """A separate app+client with rate limiting actually turned on.

    Flask-Limiter reads RATELIMIT_ENABLED once, inside init_app() at app
    creation time — mutating app.config afterward (which is what the
    shared `app` fixture would require, since TestingConfig disables it
    by default) has no effect, so this builds its own app from a
    monkeypatched config instead of trying to flip the flag post-hoc.
    """
    monkeypatch.setattr(TestingConfig, "RATELIMIT_ENABLED", True)
    app = create_app("testing")
    with app.app_context():
        _db.create_all()
        try:
            limiter.reset()
        except AssertionError:
            pass
        yield app.test_client()
        _db.session.remove()
        _db.drop_all()


def test_register_creates_user(client, app):
    resp = client.post(
        "/register",
        data={
            "username": "bob",
            "password": "password123",
            "email": "bob@example.com",
            "first_name": "Bob",
            "last_name": "Smith",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert User.query.filter_by(username="bob").first() is not None


def test_register_rejects_short_password(client):
    resp = client.post(
        "/register",
        data={"username": "bob", "password": "short", "email": "bob@example.com"},
        follow_redirects=True,
    )
    assert User.query.filter_by(username="bob").first() is None
    assert b"at least 8 characters" in resp.data


def test_register_rejects_duplicate_username(client, user):
    resp = client.post(
        "/register",
        data={"username": user.username, "password": "password123", "email": "someoneelse@example.com"},
        follow_redirects=True,
    )
    assert b"already exists" in resp.data


def test_login_success_redirects_to_dashboard(client, user):
    resp = client.post(
        "/login",
        data={"username": user.username, "password": "password123"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"


def test_login_wrong_password_shows_error(client, user):
    resp = client.post(
        "/login",
        data={"username": user.username, "password": "wrong-password"},
        follow_redirects=True,
    )
    assert b"Invalid credentials" in resp.data


def test_login_sets_last_login_at(client, user):
    assert user.last_login_at is None
    client.post("/login", data={"username": user.username, "password": "password123"})
    refreshed = User.query.filter_by(username=user.username).first()
    assert refreshed.last_login_at is not None


def test_logout_requires_login(client):
    resp = client.get("/logout", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_logout_ends_session(auth_client):
    resp = auth_client.get("/")
    assert resp.status_code == 200

    auth_client.get("/logout")
    resp = auth_client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_login_is_rate_limited_after_repeated_failures(rate_limited_client):
    make_user(username="carol", password="realpassword123")

    for _ in range(5):
        rate_limited_client.post(
            "/login", data={"username": "carol", "password": "wrong"}, follow_redirects=True
        )

    # The 6th attempt within the window is blocked outright, even with the
    # *correct* password — proves this throttles by request volume, not
    # by "is the password right," which is the property that actually
    # stops a brute-force script.
    resp = rate_limited_client.post(
        "/login", data={"username": "carol", "password": "realpassword123"}, follow_redirects=True
    )
    assert b"Too many attempts" in resp.data


def test_login_page_views_are_never_rate_limited(rate_limited_client):
    for _ in range(10):
        resp = rate_limited_client.get("/login")
        assert resp.status_code == 200
