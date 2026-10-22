"""Savings goals, spotted bills and category rollover (2026-10): the shared
services, the web Plan routes and the native app's API."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from artha.extensions import db
from artha.models import CategoryBudget, DismissedBillSuggestion, Goal, Transaction
from artha.models.api_token import ApiToken
from artha.services.goals import describe_goal, months_until
from artha.services.recurring_detect import suggest_bills
from artha.services.screens import plan_screen

from .conftest import current_period_timestamp

AJAX = {"X-Requested-With": "XMLHttpRequest"}


def _tx(user, description, amount, when: date, category=None, recurring=False, t_type="expense"):
    tx = Transaction(
        description=description,
        amount=Decimal(amount),
        type=t_type,
        user_id=user.id,
        category=category,
        timestamp=datetime(when.year, when.month, when.day, 12, 0, tzinfo=timezone.utc),
        is_recurring=recurring,
    )
    db.session.add(tx)
    db.session.commit()
    return tx


def _bearer(user):
    _, raw = ApiToken.issue(user, "Test iPhone")
    db.session.commit()
    return {"Authorization": f"Bearer {raw}"}


# --- Goals -------------------------------------------------------------------

def test_months_until_counts_this_month():
    today = date(2026, 10, 21)
    assert months_until(today, date(2026, 10, 30)) == 1
    assert months_until(today, date(2026, 12, 1)) == 3
    assert months_until(today, date(2026, 9, 1)) == 0


def test_goal_per_month_spreads_whats_left_over_the_months(app, user):
    goal = Goal(user_id=user.id, name="Trip", target_amount=Decimal("1200"), saved_amount=Decimal("200"),
                currency="USD", target_date=date(2027, 1, 15))
    db.session.add(goal)
    db.session.commit()
    view = describe_goal(goal, date(2026, 10, 21))
    # $1,000 left over Oct, Nov, Dec, Jan.
    assert view["per_month"] == "250.00"
    assert view["percent"] == 16
    assert view["status"] == "saving"


def test_goal_is_done_once_saved_reaches_target(app, user):
    goal = Goal(user_id=user.id, name="Laptop", target_amount=Decimal("900"), saved_amount=Decimal("950"), currency="USD")
    db.session.add(goal)
    db.session.commit()
    view = describe_goal(goal, date(2026, 10, 21))
    assert view["status"] == "done"
    assert view["percent"] == 100
    assert view["per_month"] is None


def test_web_goal_lifecycle(auth_client, user):
    resp = auth_client.post("/plan/goals", json={"name": "Emergency fund", "target_amount": "3000"}, headers=AJAX)
    assert resp.status_code == 201
    goal_id = resp.get_json()["goal"]["id"]

    resp = auth_client.post(f"/plan/goals/{goal_id}/add", json={"amount": 500}, headers=AJAX)
    assert resp.get_json()["goal"]["saved"] == "500.00"
    resp = auth_client.post(f"/plan/goals/{goal_id}/add", json={"amount": -800}, headers=AJAX)
    assert resp.get_json()["goal"]["saved"] == "0.00"  # never below zero

    resp = auth_client.post(f"/plan/goals/{goal_id}", json={"name": "Rainy day"}, headers=AJAX)
    assert resp.get_json()["goal"]["name"] == "Rainy day"
    assert resp.get_json()["goal"]["target"] == "3000.00"

    body = auth_client.get("/plan").get_data(as_text=True)
    assert "Rainy day" in body

    assert auth_client.post(f"/plan/goals/{goal_id}/delete", headers=AJAX).status_code == 200
    assert Goal.query.count() == 0


def test_goal_needs_a_name_and_amount(auth_client):
    resp = auth_client.post("/plan/goals", json={"name": "", "target_amount": "10"}, headers=AJAX)
    assert resp.status_code == 400
    resp = auth_client.post("/plan/goals", json={"name": "Trip", "target_amount": "0"}, headers=AJAX)
    assert resp.status_code == 400


def test_renaming_a_goal_keeps_its_currency(auth_client, user):
    resp = auth_client.post("/plan/goals", json={"name": "Trip", "target_amount": "100"}, headers=AJAX)
    goal_id = resp.get_json()["goal"]["id"]
    user.preferred_currency = "GBP"
    db.session.commit()
    auth_client.post(f"/plan/goals/{goal_id}", json={"name": "Big trip"}, headers=AJAX)
    assert db.session.get(Goal, goal_id).currency == "USD"


def test_someone_elses_goal_is_not_found(auth_client, user):
    from .conftest import make_user

    other = make_user(username="goal-other", password="password123")
    goal = Goal(user_id=other.id, name="Theirs", target_amount=Decimal("10"), saved_amount=Decimal("0"), currency="USD")
    db.session.add(goal)
    db.session.commit()
    assert auth_client.post(f"/plan/goals/{goal.id}/add", json={"amount": 5}, headers=AJAX).status_code == 404


def test_api_goal_endpoints(client, user):
    headers = _bearer(user)
    resp = client.post("/api/v1/goals", json={"name": "Car", "target_amount": "5000", "target_date": "2027-10-01"}, headers=headers)
    assert resp.status_code == 201
    goal_id = resp.get_json()["goal"]["id"]
    resp = client.post(f"/api/v1/goals/{goal_id}/add", json={"amount": "100"}, headers=headers)
    assert resp.get_json()["goal"]["saved"] == "100.00"
    resp = client.patch(f"/api/v1/goals/{goal_id}", json={"target_date": ""}, headers=headers)
    assert resp.get_json()["goal"]["target_date"] is None
    plan = client.get("/api/v1/plan", headers=headers).get_json()
    assert [g["name"] for g in plan["goals"]] == ["Car"]
    assert client.delete(f"/api/v1/goals/{goal_id}", headers=headers).status_code == 204


def test_deleting_a_user_deletes_their_goals(app, user):
    db.session.add(Goal(user_id=user.id, name="X", target_amount=Decimal("1"), saved_amount=Decimal("0"), currency="USD"))
    db.session.add(DismissedBillSuggestion(user_id=user.id, merchant_key="netflix"))
    db.session.commit()
    db.session.delete(user)
    db.session.commit()
    assert Goal.query.count() == 0
    assert DismissedBillSuggestion.query.count() == 0


# --- Spotted bills -------------------------------------------------------------

def _monthly(user, description, amount, today, count=3, category="subscriptions"):
    for k in range(count):
        _tx(user, description, amount, today - timedelta(days=5 + 30 * k), category=category)


def test_a_steady_monthly_charge_is_suggested(app, user):
    today = date.today()
    _monthly(user, "NETFLIX.COM 866-579", "15.49", today)
    suggestions = suggest_bills(user, today)
    assert len(suggestions) == 1
    assert suggestions[0]["interval"] == "monthly"
    assert suggestions[0]["amount"] == "15.49"


def test_irregular_or_varying_charges_are_not_suggested(app, user):
    today = date.today()
    # Coffee: frequent but irregular.
    for days_ago in (1, 2, 6, 13, 15, 29):
        _tx(user, "Blue Bottle", "6.75", today - timedelta(days=days_ago))
    # Groceries: monthly-ish but the amount swings.
    for k, amount in enumerate(("40", "120", "75")):
        _tx(user, "Whole Foods", amount, today - timedelta(days=4 + 30 * k))
    assert suggest_bills(user, today) == []


def test_a_bill_that_stopped_is_not_suggested(app, user):
    today = date.today()
    for k in range(3):
        _tx(user, "Old gym", "30", today - timedelta(days=90 + 30 * k))
    assert suggest_bills(user, today) == []


def test_already_tracked_and_dismissed_merchants_are_skipped(app, user):
    today = date.today()
    _monthly(user, "Spotify", "11.99", today)
    _tx(user, "Spotify", "11.99", today - timedelta(days=200), recurring=True)
    _monthly(user, "Hulu", "7.99", today)
    db.session.add(DismissedBillSuggestion(user_id=user.id, merchant_key="hulu"))
    db.session.commit()
    assert suggest_bills(user, today) == []


def test_tracking_a_suggestion_makes_the_latest_charge_recurring(auth_client, user):
    today = date.today()
    _monthly(user, "Netflix", "15.49", today)
    key = suggest_bills(user, today)[0]["key"]
    resp = auth_client.post("/plan/bills/track", json={"key": key}, headers=AJAX)
    assert resp.status_code == 200
    latest = Transaction.query.filter_by(description="Netflix").order_by(Transaction.timestamp.desc()).first()
    assert latest.is_recurring is True
    assert latest.recurrence_interval is None  # monthly
    assert suggest_bills(user, today) == []


def test_dismissing_a_suggestion_through_the_api(client, user):
    today = date.today()
    _monthly(user, "Netflix", "15.49", today)
    headers = _bearer(user)
    plan = client.get("/api/v1/plan", headers=headers).get_json()
    key = plan["bill_suggestions"][0]["key"]
    assert client.get("/api/v1/today", headers=headers).get_json()["bill_suggestions"] == 1
    assert client.post("/api/v1/bill-suggestions/dismiss", json={"key": key}, headers=headers).status_code == 204
    assert client.get("/api/v1/plan", headers=headers).get_json()["bill_suggestions"] == []


# --- Rollover ------------------------------------------------------------------

def _last_month_day(today: date) -> date:
    return today.replace(day=1) - timedelta(days=10)


def test_rollover_carries_last_months_leftover(app, user):
    today = date.today()
    db.session.add(CategoryBudget(user_id=user.id, category="dining", monthly_cap=Decimal("200"), currency="USD", rollover=True))
    db.session.commit()
    _tx(user, "Dinner", "150", _last_month_day(today), category="dining")
    _tx(user, "Lunch", "20", current_period_timestamp().date(), category="dining")

    limit = plan_screen(user)["category_budgets"][0]
    assert limit["carried"] == "50.00"
    assert limit["cap"] == "250.00"
    assert limit["left"] == "230.00"


def test_rollover_takes_off_last_months_overspending(app, user):
    today = date.today()
    db.session.add(CategoryBudget(user_id=user.id, category="dining", monthly_cap=Decimal("200"), currency="USD", rollover=True))
    db.session.commit()
    _tx(user, "Dinner", "260", _last_month_day(today), category="dining")

    limit = plan_screen(user)["category_budgets"][0]
    assert limit["carried"] == "-60.00"
    assert limit["cap"] == "140.00"


def test_a_limit_without_rollover_ignores_last_month(app, user):
    today = date.today()
    db.session.add(CategoryBudget(user_id=user.id, category="dining", monthly_cap=Decimal("200"), currency="USD"))
    db.session.commit()
    _tx(user, "Dinner", "20", _last_month_day(today), category="dining")
    limit = plan_screen(user)["category_budgets"][0]
    assert limit["carried"] is None
    assert limit["cap"] == "200.00"


def test_a_limit_wiped_out_by_rollover_still_shows_as_over(app, user):
    today = date.today()
    db.session.add(CategoryBudget(user_id=user.id, category="dining", monthly_cap=Decimal("100"), currency="USD", rollover=True))
    db.session.commit()
    _tx(user, "Party", "350", _last_month_day(today), category="dining")
    limit = plan_screen(user)["category_budgets"][0]
    assert limit["tier"] == "over"


def test_rollover_is_saved_only_when_sent(auth_client, user):
    auth_client.post("/finance/category-budget", data={"category": "dining", "monthly_cap": "200", "rollover": "1"}, headers=AJAX)
    assert CategoryBudget.query.first().rollover is True
    auth_client.post("/finance/category-budget", data={"category": "dining", "monthly_cap": "250"}, headers=AJAX)
    assert CategoryBudget.query.first().rollover is True
    auth_client.post("/finance/category-budget", data={"category": "dining", "monthly_cap": "250", "rollover": "0"}, headers=AJAX)
    assert CategoryBudget.query.first().rollover is False


# --- Editing a transaction's series -------------------------------------------

def test_an_edit_can_start_and_stop_a_series(auth_client, user):
    tx = _tx(user, "Gym", "30", current_period_timestamp().date())
    resp = auth_client.post(f"/update_transaction/{tx.id}", json={"is_recurring": True, "recurrence_interval": "weekly",
                                                                  "recurring_end_date": "2027-01-31"}, headers=AJAX)
    assert resp.status_code == 200
    db.session.refresh(tx)
    assert tx.is_recurring is True
    assert tx.recurrence_interval == "weekly"
    assert tx.recurring_end_date == date(2027, 1, 31)

    older = _tx(user, "Gym", "30", current_period_timestamp().date() - timedelta(days=40), recurring=True)
    auth_client.post(f"/update_transaction/{tx.id}", json={"is_recurring": False}, headers=AJAX)
    db.session.refresh(tx)
    db.session.refresh(older)
    assert tx.is_recurring is False
    assert older.is_recurring is False  # the whole series stops


def test_adding_with_an_end_date(auth_client, user):
    resp = auth_client.post("/add_transaction", data={
        "description": "Course", "amount": "40", "type": "expense",
        "is_recurring": "1", "recurring_end_date": "2027-03-01",
    }, headers=AJAX)
    assert resp.status_code == 200
    assert resp.get_json()["message"] == "Transaction added!"
    tx = Transaction.query.filter_by(description="Course").first()
    assert tx.recurring_end_date == date(2027, 3, 1)


# --- Activity and Settings pages ----------------------------------------------

def test_activity_offers_categorizing_and_a_month_picker(auth_client, user):
    _tx(user, "Mystery shop", "12", current_period_timestamp().date())
    body = auth_client.get("/activity").get_data(as_text=True)
    assert "1 transaction has no category" in body
    assert "data-month-filter" in body
    assert 'id="csv-import-btn"' in body


def test_settings_page_has_everything_in_one_place(auth_client):
    body = auth_client.get("/settings").get_data(as_text=True)
    for piece in ('id="currency-select"', 'name="appearance"', 'id="toggle-push"', 'id="copy-calendar-link-btn"', "Sign out"):
        assert piece in body, piece


def test_reports_keeps_only_the_charts(auth_client):
    body = auth_client.get("/finance").get_data(as_text=True)
    assert 'id="fp-tab-spending"' in body
    assert 'id="fp-tab-overview"' not in body
    assert 'id="fp-tab-transactions"' not in body
