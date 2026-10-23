"""
The pace heads-up (finance.routes._check_pace_heads_up): one push partway
through the month when spending runs ahead of an even pace, for someone
who turned it on. Off by default; Today offers it once (pace_prompt).
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from artha.blueprints.finance.routes import _check_pace_heads_up
from artha.extensions import db
from artha.models import PushSubscription, Transaction
from artha.models.budget import Budget

# A fixed day partway through a 31-day month, so the pace math is exact:
# even pace on a $3,000 budget is $1,451.61 by the 15th, and "ahead"
# starts $150 (5%) past that.
DAY = date(2026, 3, 15)


def _setup(user, *, spent="2000", on=True, subscribed=True):
    user.notify_pace_heads_up = on
    db.session.add(Budget(user_id=user.id, monthly_cap=Decimal("3000"), currency="USD"))
    if subscribed:
        db.session.add(PushSubscription(user_id=user.id, endpoint="https://example.com/x", p256dh="a", auth="b"))
    _spend(user, spent, DAY)
    db.session.commit()


def _spend(user, amount, day):
    db.session.add(Transaction(description="Groceries", amount=Decimal(amount), type="expense", user_id=user.id,
                               currency="USD", usd_value=Decimal(amount),
                               timestamp=datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)))
    db.session.commit()


def _run(user, day=DAY):
    calls = []

    def _send(sub, title, body, url="/"):
        calls.append({"title": title, "body": body, "url": url})
        return "sent"

    with patch("artha.services.month_snapshot.user_today", return_value=day), \
            patch("artha.blueprints.finance.routes.send_push", side_effect=_send):
        _check_pace_heads_up(user.id)
    return calls


def test_ahead_of_pace_sends_one_heads_up_a_month(app, user):
    _setup(user)

    first = _run(user)
    second = _run(user)

    assert len(first) == 1 and second == []
    assert first[0]["title"] == "Heads-up" and first[0]["url"] == "/plan"
    # $1,000 left over the 17 days that remain, today included.
    assert first[0]["body"] == "You're ahead of pace for March. Up to $58 a day keeps it on plan."
    assert Budget.query.filter_by(user_id=user.id).one().pace_alerted_month == date(2026, 3, 1)


def test_off_unless_turned_on(app, user):
    _setup(user, on=False)
    assert _run(user) == []


def test_on_pace_or_early_in_the_month_says_nothing(app, user):
    _setup(user, spent="1500")  # within 5% of even pace
    assert _run(user) == []

    early = date(2026, 3, 4)
    _spend(user, "800", early)
    assert _run(user, day=early) == []  # ahead, but only the 4th


def test_over_budget_is_the_overage_alerts_job_not_this(app, user):
    _setup(user, spent="3200")
    assert _run(user) == []


def test_getting_back_on_pace_rearms_it(app, user):
    _setup(user)
    assert len(_run(user)) == 1

    # A week later with no more spending, the even pace has caught up.
    later = date(2026, 3, 22)
    assert _run(user, day=later) == []
    assert Budget.query.filter_by(user_id=user.id).one().pace_alerted_month is None
    # Drifting ahead again warns again.
    _spend(user, "800", later)
    assert len(_run(user, day=later)) == 1


def test_no_push_subscription_means_nothing_is_marked_sent(app, user):
    _setup(user, subscribed=False)
    assert _run(user) == []
    assert Budget.query.filter_by(user_id=user.id).one().pace_alerted_month is None


def test_a_save_runs_the_check(auth_client, user):
    _setup(user, spent="10")
    calls = []

    def _send(sub, title, body, url="/"):
        calls.append(title)
        return "sent"

    with patch("artha.services.month_snapshot.user_today", return_value=DAY), \
            patch("artha.blueprints.finance.routes.send_push", side_effect=_send):
        auth_client.post("/add_transaction", data={"description": "TV", "amount": "1990", "type": "expense",
                                                    "date": DAY.isoformat()})
    assert "Heads-up" in calls


def test_today_offers_it_once_and_only_with_a_budget(app, user, client):
    from artha.services.screens import today_screen

    assert today_screen(user)["pace_prompt"] is False  # no budget yet
    db.session.add(Budget(user_id=user.id, monthly_cap=Decimal("3000"), currency="USD"))
    db.session.commit()
    assert today_screen(user)["pace_prompt"] is True

    token = client.post("/api/v1/sessions", json={"username": user.username, "password": "password123"}).get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    me = client.patch("/api/v1/me", headers=headers, json={"pace_prompt_answered": True}).get_json()["user"]
    assert me["notify_pace_heads_up"] is False
    db.session.refresh(user)
    assert today_screen(user)["pace_prompt"] is False


def test_the_web_card_and_settings_save_the_choice(auth_client, user):
    resp = auth_client.post("/push/preferences", json={"notify_pace_heads_up": True, "pace_prompt_answered": True})
    assert resp.status_code == 200
    db.session.refresh(user)
    assert user.notify_pace_heads_up and user.pace_prompt_answered
