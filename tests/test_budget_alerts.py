"""
Push alerts fired the moment a transaction save first pushes a budget
(or a category budget) over its monthly cap -- artha.blueprints.finance.
routes._check_budget_overage_alerts, wired into add_transaction,
update_transaction, generate_recurring, and import_commit.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from artha.extensions import db
from artha.models import PushSubscription
from artha.models.budget import Budget
from artha.models.category_budget import CategoryBudget

from .conftest import current_period_timestamp


def _subscribe(user):
    sub = PushSubscription(user_id=user.id, endpoint="https://example.com/x", p256dh="a", auth="b")
    db.session.add(sub)
    db.session.commit()
    return sub


def _fake_send_push(calls):
    def _send(sub, title, body, url="/"):
        calls.append({"title": title, "body": body})
        return "sent"
    return _send


def test_add_transaction_alerts_the_moment_it_crosses_over_budget(auth_client, user):
    _subscribe(user)
    db.session.add(Budget(user_id=user.id, monthly_cap=Decimal("100")))
    db.session.commit()

    calls = []
    with patch("artha.blueprints.finance.routes.send_push", side_effect=_fake_send_push(calls)):
        resp = auth_client.post(
            "/add_transaction",
            data={"description": "Rent", "amount": "150", "type": "expense"},
            follow_redirects=True,
        )

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0]["title"] == "Budget alert"
    assert "$100.00" in calls[0]["body"]

    row = Budget.query.filter_by(user_id=user.id).first()
    assert row.alerted_month == date(date.today().year, date.today().month, 1)


def test_no_alert_while_still_under_budget(auth_client, user):
    _subscribe(user)
    db.session.add(Budget(user_id=user.id, monthly_cap=Decimal("500")))
    db.session.commit()

    calls = []
    with patch("artha.blueprints.finance.routes.send_push", side_effect=_fake_send_push(calls)):
        auth_client.post(
            "/add_transaction",
            data={"description": "Coffee", "amount": "5", "type": "expense"},
            follow_redirects=True,
        )

    assert calls == []


def test_only_alerts_once_per_month_not_every_transaction_after(auth_client, user):
    _subscribe(user)
    db.session.add(Budget(user_id=user.id, monthly_cap=Decimal("100")))
    db.session.commit()

    calls = []
    with patch("artha.blueprints.finance.routes.send_push", side_effect=_fake_send_push(calls)):
        auth_client.post(
            "/add_transaction",
            data={"description": "First", "amount": "150", "type": "expense"},
            follow_redirects=True,
        )
        auth_client.post(
            "/add_transaction",
            data={"description": "Second", "amount": "20", "type": "expense"},
            follow_redirects=True,
        )

    assert len(calls) == 1


def test_income_transactions_never_trigger_a_budget_check(auth_client, user):
    _subscribe(user)
    db.session.add(Budget(user_id=user.id, monthly_cap=Decimal("1")))
    db.session.commit()

    calls = []
    with patch("artha.blueprints.finance.routes.send_push", side_effect=_fake_send_push(calls)):
        auth_client.post(
            "/add_transaction",
            data={"description": "Paycheck", "amount": "9000", "type": "income"},
            follow_redirects=True,
        )

    assert calls == []


def test_category_budget_alerts_independently_of_the_overall_one(auth_client, user):
    _subscribe(user)
    db.session.add(CategoryBudget(user_id=user.id, category="dining", monthly_cap=Decimal("50")))
    db.session.commit()

    calls = []
    with patch("artha.blueprints.finance.routes.send_push", side_effect=_fake_send_push(calls)):
        auth_client.post(
            "/add_transaction",
            data={"description": "Dinner", "amount": "75", "type": "expense", "category": "dining"},
            follow_redirects=True,
        )

    assert len(calls) == 1
    assert "Dining" in calls[0]["body"]


def test_disabling_the_preference_suppresses_the_alert(auth_client, user):
    _subscribe(user)
    db.session.add(Budget(user_id=user.id, monthly_cap=Decimal("100")))
    user.notify_budget_alerts = False
    db.session.commit()

    calls = []
    with patch("artha.blueprints.finance.routes.send_push", side_effect=_fake_send_push(calls)):
        auth_client.post(
            "/add_transaction",
            data={"description": "Rent", "amount": "150", "type": "expense"},
            follow_redirects=True,
        )

    assert calls == []


def test_no_push_subscription_means_no_attempted_send(auth_client, user):
    db.session.add(Budget(user_id=user.id, monthly_cap=Decimal("100")))
    db.session.commit()

    calls = []
    with patch("artha.blueprints.finance.routes.send_push", side_effect=_fake_send_push(calls)):
        resp = auth_client.post(
            "/add_transaction",
            data={"description": "Rent", "amount": "150", "type": "expense"},
            follow_redirects=True,
        )

    assert resp.status_code == 200
    assert calls == []


def test_a_gone_subscription_is_pruned_during_the_alert_send(auth_client, user):
    sub = _subscribe(user)
    db.session.add(Budget(user_id=user.id, monthly_cap=Decimal("100")))
    db.session.commit()

    def _send_gone(sub, title, body, url="/"):
        return "gone"

    with patch("artha.blueprints.finance.routes.send_push", side_effect=_send_gone):
        auth_client.post(
            "/add_transaction",
            data={"description": "Rent", "amount": "150", "type": "expense"},
            follow_redirects=True,
        )

    assert db.session.get(PushSubscription, sub.id) is None


def test_set_preferences_updates_notify_budget_alerts(auth_client, user):
    resp = auth_client.post("/push/preferences", json={"notify_budget_alerts": False})
    assert resp.status_code == 200
    assert user.notify_budget_alerts is False
