"""Deleting a User must take every row that belongs to that user with it.

The Conversation/Message side of this is covered in test_ai_conversation.py
(test_deleting_a_user_cascades_to_their_conversations_and_messages). This
file guards the rest. Event, PushSubscription, and Scenario had their
user_id relationship declared with no cascade and left orphaned rows behind
on a user delete; Budget, CategoryBudget, and Feedback had the exact same
gap, found while designing account deletion (2026-08-27) and fixed
alongside it.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from artha.extensions import db
from artha.models import Event, PushSubscription, Feedback
from artha.models.budget import Budget
from artha.models.category_budget import CategoryBudget
from artha.models.scenario import Scenario


def test_deleting_a_user_removes_their_events_subscriptions_and_scenarios(app, user):
    now = datetime.now(timezone.utc)

    db.session.add(
        Event(
            user_id=user.id,
            title="Dentist",
            start=now,
            end=now + timedelta(hours=1),
        )
    )
    db.session.add(
        PushSubscription(
            user_id=user.id,
            endpoint="https://push.example/abc",
            p256dh="key",
            auth="auth",
        )
    )
    db.session.add(
        Scenario(
            user_id=user.id,
            title="Move apartments",
            monthly_cost=Decimal("100"),
            monthly_savings=Decimal("0"),
        )
    )
    db.session.add(Budget(user_id=user.id, monthly_cap=Decimal("2000")))
    db.session.add(CategoryBudget(user_id=user.id, category="dining", monthly_cap=Decimal("300")))
    db.session.add(Feedback(user_id=user.id, category="bug", message="Something broke"))
    db.session.commit()

    user_id = user.id

    db.session.delete(user)
    db.session.commit()

    assert Event.query.filter_by(user_id=user_id).count() == 0
    assert PushSubscription.query.filter_by(user_id=user_id).count() == 0
    assert Scenario.query.filter_by(user_id=user_id).count() == 0
    assert Budget.query.filter_by(user_id=user_id).count() == 0
    assert CategoryBudget.query.filter_by(user_id=user_id).count() == 0
    assert Feedback.query.filter_by(user_id=user_id).count() == 0

    # No orphans anywhere in these tables (belt-and-braces: the user_id
    # filters above would already have caught a dangling row).
    assert Event.query.count() == 0
    assert PushSubscription.query.count() == 0
    assert Scenario.query.count() == 0
    assert Budget.query.count() == 0
    assert CategoryBudget.query.count() == 0
    assert Feedback.query.count() == 0
