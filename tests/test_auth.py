from artha.models import User

from .conftest import make_user


def test_privacy_page_is_reachable_without_logging_in(client):
    resp = client.get("/privacy")
    assert resp.status_code == 200
    assert b"Privacy" in resp.data


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


def test_register_rejects_malformed_email(client):
    resp = client.post(
        "/register",
        data={"username": "bob", "password": "password123", "email": "not-an-email"},
        follow_redirects=True,
    )
    assert User.query.filter_by(username="bob").first() is None
    assert b"look like a valid email" in resp.data


def test_edit_profile_requires_login(client):
    resp = client.get("/profile", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_edit_profile_updates_email_and_name(auth_client, user):
    resp = auth_client.post(
        "/profile",
        data={"first_name": "Tamkin", "last_name": "Anwar", "email": "tamkinanwar7@gmail.com"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    refreshed = User.query.filter_by(username=user.username).first()
    assert refreshed.email == "tamkinanwar7@gmail.com"
    assert refreshed.first_name == "Tamkin"
    assert refreshed.last_name == "Anwar"


def test_edit_profile_keeps_username_unchanged(auth_client, user):
    original_username = user.username
    auth_client.post(
        "/profile",
        data={"first_name": "New", "email": "new@example.com"},
        follow_redirects=True,
    )
    refreshed = User.query.filter_by(username=original_username).first()
    assert refreshed is not None
    assert refreshed.username == original_username


def test_edit_profile_rejects_malformed_email(auth_client, user):
    resp = auth_client.post(
        "/profile",
        data={"first_name": "Bob", "email": "not-an-email"},
        follow_redirects=True,
    )
    refreshed = User.query.filter_by(username=user.username).first()
    assert refreshed.email != "not-an-email"
    assert b"look like a valid email" in resp.data


def test_edit_profile_rejects_email_already_used_by_another_account(auth_client, user, app):
    other = make_user(username="other-user", email="taken@example.com")
    resp = auth_client.post(
        "/profile",
        data={"first_name": "Bob", "email": "taken@example.com"},
        follow_redirects=True,
    )
    refreshed = User.query.filter_by(username=user.username).first()
    assert refreshed.email != "taken@example.com"
    assert b"already in use" in resp.data


def test_edit_profile_allows_keeping_own_current_email(auth_client, user):
    resp = auth_client.post(
        "/profile",
        data={"first_name": "Bob", "email": user.email},
        follow_redirects=True,
    )
    assert b"Profile updated" in resp.data
