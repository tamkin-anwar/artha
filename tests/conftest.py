import pytest

from artha import create_app
from artha.config import TestingConfig
from artha.extensions import db as _db
from artha.extensions import limiter
from artha.models import User


@pytest.fixture()
def app():
    """A fresh app + in-memory DB per test. The app context is kept open
    for the whole test (not just setup) so ORM objects returned by
    fixtures stay usable without DetachedInstanceError."""
    app = create_app("testing")
    ctx = app.app_context()
    ctx.push()
    _db.create_all()
    yield app
    _db.session.remove()
    _db.drop_all()
    ctx.pop()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def _reset_limiter(app):
    # Flask-Limiter's in-memory storage lives on the shared `limiter`
    # object (module-level singleton), not per-app — without this, hits
    # from earlier tests would silently carry into later ones since every
    # test client "connects" from the same fixed IP in the same process.
    # Storage is only lazily initialized when RATELIMIT_ENABLED is true
    # (most tests leave it False via TestingConfig), so .reset() has
    # nothing to reset yet on those — that's fine, skip it.
    try:
        limiter.reset()
    except AssertionError:
        pass
    yield


def make_user(username="alice", password="password123", email=None, **extra):
    user = User(username=username, email=email or f"{username}@example.com", **extra)
    user.set_password(password)
    _db.session.add(user)
    _db.session.commit()
    return user


@pytest.fixture()
def user(app):
    return make_user()


@pytest.fixture()
def auth_client(client, user):
    """A test client already logged in as `user` (password: password123)."""
    client.post("/login", data={"username": user.username, "password": "password123"})
    return client


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
