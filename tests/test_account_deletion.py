"""Account deletion: a 30-day soft-delete grace period, canceled simply by
logging back in, with a hard purge (CLI command + an opportunistic sweep on
every /login page load) once that window passes. See
blueprints/auth/routes.py's delete_account()/verify_2fa()/login() and
_purge_expired_deleted_accounts(), and cli.py's purge-expired-accounts.
"""

from datetime import datetime, timedelta, timezone

from artha.extensions import db
from artha.models import User
from artha.models.finance import Transaction

from .conftest import make_user


def test_delete_account_requires_correct_password(auth_client, user):
    resp = auth_client.post(
        "/account/delete",
        data={"password": "wrong-password", "confirm_username": user.username},
        follow_redirects=True,
    )
    assert b"incorrect" in resp.data
    assert User.query.filter_by(username=user.username).first().deleted_at is None


def test_delete_account_requires_username_confirmation(auth_client, user):
    resp = auth_client.post(
        "/account/delete",
        data={"password": "password123", "confirm_username": "not-my-username"},
        follow_redirects=True,
    )
    assert b"Type your username" in resp.data
    assert User.query.filter_by(username=user.username).first().deleted_at is None


def test_delete_account_sets_deleted_at_and_logs_out(auth_client, user):
    resp = auth_client.post(
        "/account/delete",
        data={"password": "password123", "confirm_username": user.username},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    refreshed = User.query.filter_by(username=user.username).first()
    assert refreshed.deleted_at is not None

    # Logged out: the dashboard now redirects to login instead of loading.
    dash_resp = auth_client.get("/")
    assert dash_resp.status_code == 302
    assert "/login" in dash_resp.headers["Location"]


def test_delete_account_blocks_the_sole_remaining_admin(client, app):
    admin = make_user(username="only-admin", password="password123", is_admin=True)
    client.post("/login", data={"username": admin.username, "password": "password123"})

    resp = client.post(
        "/account/delete",
        data={"password": "password123", "confirm_username": admin.username},
        follow_redirects=True,
    )
    assert b"only admin" in resp.data
    assert User.query.filter_by(username=admin.username).first().deleted_at is None


def test_delete_account_allowed_when_another_admin_exists(client, app):
    make_user(username="other-admin", password="password123", is_admin=True)
    admin = make_user(username="deleting-admin", password="password123", is_admin=True)
    client.post("/login", data={"username": admin.username, "password": "password123"})

    resp = client.post(
        "/account/delete",
        data={"password": "password123", "confirm_username": admin.username},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert User.query.filter_by(username=admin.username).first().deleted_at is not None


def test_logging_back_in_during_the_window_cancels_deletion(client, user):
    user.deleted_at = datetime.now(timezone.utc) - timedelta(days=5)
    db.session.commit()

    resp = client.post(
        "/login",
        data={"username": user.username, "password": "password123"},
        follow_redirects=True,
    )
    assert b"deletion canceled" in resp.data.lower() or b"welcome back" in resp.data.lower()

    refreshed = User.query.filter_by(username=user.username).first()
    assert refreshed.deleted_at is None

    # Actually logged in, not just redirected — the dashboard loads.
    dash_resp = client.get("/")
    assert dash_resp.status_code == 200


def test_purge_expired_accounts_cli_command_cascades_and_respects_the_window(app, user):
    other = make_user(username="expired-user", password="password123")
    db.session.add(Transaction(
        user_id=other.id, description="Coffee", amount=5, type="expense",
    ))
    db.session.commit()
    other.deleted_at = datetime.now(timezone.utc) - timedelta(days=31)
    user.deleted_at = datetime.now(timezone.utc) - timedelta(days=5)  # still inside the window
    db.session.commit()

    other_id, user_id = other.id, user.id

    result = app.test_cli_runner().invoke(args=["purge-expired-accounts"])

    assert result.exit_code == 0
    assert "Purged 1 account" in result.output
    assert db.session.get(User, other_id) is None
    # The transaction cascaded away with the account, not left orphaned.
    assert Transaction.query.filter_by(user_id=other_id).count() == 0
    # Still inside its own window: untouched.
    assert db.session.get(User, user_id) is not None


def test_login_page_load_opportunistically_purges_expired_accounts(client, app):
    other = make_user(username="expired-user-2", password="password123")
    other.deleted_at = datetime.now(timezone.utc) - timedelta(days=31)
    db.session.commit()
    other_id = other.id

    client.get("/login")

    assert db.session.get(User, other_id) is None
