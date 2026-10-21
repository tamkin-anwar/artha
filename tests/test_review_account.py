"""services/review_account.py: the sample account App Review signs in with."""

from artha.extensions import db
from artha.models import Transaction, User
from artha.models.account import Account
from artha.services.month_snapshot import build_month_snapshot
from artha.services.review_account import REVIEW_USERNAME, create_review_account


def test_review_account_has_something_on_every_tab(app):
    user, password = create_review_account()

    assert user.check_password(password)
    assert len(password) >= 16
    snap = build_month_snapshot(user)
    assert snap.safe_to_spend is not None
    assert snap.expense > 0
    assert Transaction.query.filter_by(user_id=user.id, is_recurring=True).count() >= 3
    assert Account.query.filter_by(user_id=user.id).count() == 5


def test_running_it_again_replaces_the_account_and_password(app):
    _, first_password = create_review_account()
    seeded = Transaction.query.count()
    second, second_password = create_review_account()

    assert User.query.filter_by(username=REVIEW_USERNAME).count() == 1
    assert second_password != first_password
    assert not second.check_password(first_password)
    # The first account's rows are gone, not left behind next to the new ones.
    assert Transaction.query.count() == seeded
    assert Transaction.query.filter(Transaction.user_id != second.id).count() == 0


def test_the_cli_prints_the_credentials(app):
    result = app.test_cli_runner().invoke(args=["create-review-account"])
    assert result.exit_code == 0
    assert "Username: appreview" in result.output
    assert "Password: " in result.output
    db.session.expire_all()
    assert User.query.filter_by(username=REVIEW_USERNAME).count() == 1
