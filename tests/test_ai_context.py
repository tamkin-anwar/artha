from datetime import date, datetime, timedelta
from decimal import Decimal

from artha.extensions import db
from artha.models import Event, Note, Transaction
from artha.models.budget import Budget
from artha.models.category_budget import CategoryBudget
from artha.models.scenario import Scenario
from artha.services.ai_service import (
    _assemble_budget_context,
    _assemble_financial_context,
    _assemble_scenario_context,
    _assemble_upcoming_context,
)


def _expense(user, amount, category, days_ago=0):
    ts = datetime.now() - timedelta(days=days_ago)
    tx = Transaction(
        description="x", amount=Decimal(str(amount)), type="expense",
        category=category, user_id=user.id, timestamp=ts,
    )
    db.session.add(tx)
    db.session.commit()
    return tx


def test_financial_context_breaks_down_this_months_spending_by_category(app, user):
    _expense(user, "60.00", "dining", days_ago=1)
    _expense(user, "40.00", "dining", days_ago=2)
    _expense(user, "25.00", "transport", days_ago=1)

    context = _assemble_financial_context(user)
    assert "Dining: $100.00" in context
    assert "Transport: $25.00" in context


def test_budget_context_reports_no_budgets_when_none_set(app, user):
    assert _assemble_budget_context(user) == "No budgets set."


def test_budget_context_shows_overall_progress(app, user):
    db.session.add(Budget(user_id=user.id, monthly_cap=Decimal("1000")))
    db.session.commit()
    _expense(user, "250.00", "groceries", days_ago=1)

    context = _assemble_budget_context(user)
    assert "Overall monthly budget: $1,000.00" in context
    assert "$250.00 spent so far this month" in context
    assert "(25%)" in context


def test_budget_context_shows_category_progress(app, user):
    db.session.add(CategoryBudget(user_id=user.id, category="dining", monthly_cap=Decimal("400")))
    db.session.commit()
    _expense(user, "200.00", "dining", days_ago=1)

    context = _assemble_budget_context(user)
    assert "Dining budget: $400.00, $200.00 spent (50%)." in context


def test_upcoming_context_reports_nothing_when_empty(app, user):
    assert _assemble_upcoming_context(user) == "Nothing due or scheduled in the near future."


def test_upcoming_context_includes_notes_due_soon_but_not_far_out(app, user):
    today = date.today()
    db.session.add(Note(title="Renew passport", content="x", user_id=user.id, due_date=today + timedelta(days=3)))
    db.session.add(Note(title="Way later", content="x", user_id=user.id, due_date=today + timedelta(days=60)))
    db.session.add(Note(title="Done already", content="x", user_id=user.id, due_date=today + timedelta(days=1), archived=True))
    db.session.commit()

    context = _assemble_upcoming_context(user)
    assert "Renew passport" in context
    assert "Way later" not in context
    assert "Done already" not in context


def test_upcoming_context_includes_events_in_next_week_but_not_further(app, user):
    now = datetime.now()
    db.session.add(Event(user_id=user.id, title="Dentist", start=now + timedelta(days=2), end=now + timedelta(days=2, hours=1)))
    db.session.add(Event(user_id=user.id, title="Someday", start=now + timedelta(days=20), end=now + timedelta(days=20, hours=1)))
    db.session.commit()

    context = _assemble_upcoming_context(user)
    assert "Dentist" in context
    assert "Someday" not in context


def test_scenario_context_reports_none_when_empty(app, user):
    assert _assemble_scenario_context(user) == "No active scenarios."


def test_scenario_context_includes_active_but_not_archived(app, user):
    db.session.add(Scenario(
        user_id=user.id, title="New apartment", category="housing", status="active",
        one_time_cost=Decimal("500"), monthly_cost=Decimal("200"), monthly_savings=Decimal("50"),
        priority="high",
    ))
    db.session.add(Scenario(
        user_id=user.id, title="Old idea", category="other", status="archived",
    ))
    db.session.commit()

    context = _assemble_scenario_context(user)
    assert "New apartment" in context
    assert "net $-150.00/mo" in context
    assert "Old idea" not in context
