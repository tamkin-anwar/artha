from datetime import datetime, timezone
from decimal import Decimal

from artha.extensions import db
from artha.models import CategoryBudget, Transaction

from .conftest import make_user


def _add_expense(user, amount, category=None):
    tx = Transaction(
        description="Spend",
        amount=Decimal(amount),
        type="expense",
        category=category,
        user_id=user.id,
        timestamp=datetime.now(timezone.utc),
    )
    db.session.add(tx)
    db.session.commit()


def test_set_category_budget_creates_row(auth_client, user):
    resp = auth_client.post(
        "/finance/category-budget", data={"category": "dining", "monthly_cap": "300"}, follow_redirects=True
    )
    assert resp.status_code == 200
    row = CategoryBudget.query.filter_by(user_id=user.id, category="dining").first()
    assert row is not None
    assert row.monthly_cap == Decimal("300")


def test_set_category_budget_upserts_not_duplicates(auth_client, user):
    auth_client.post("/finance/category-budget", data={"category": "dining", "monthly_cap": "300"})
    auth_client.post("/finance/category-budget", data={"category": "dining", "monthly_cap": "450"})

    rows = CategoryBudget.query.filter_by(user_id=user.id, category="dining").all()
    assert len(rows) == 1
    assert rows[0].monthly_cap == Decimal("450")


def test_set_category_budget_rejects_income(auth_client, user):
    auth_client.post("/finance/category-budget", data={"category": "income", "monthly_cap": "300"})
    assert CategoryBudget.query.filter_by(user_id=user.id).count() == 0


def test_set_category_budget_rejects_unknown_category(auth_client, user):
    auth_client.post("/finance/category-budget", data={"category": "not-a-real-category", "monthly_cap": "300"})
    assert CategoryBudget.query.filter_by(user_id=user.id).count() == 0


def test_set_category_budget_rejects_zero_amount(auth_client, user):
    resp = auth_client.post(
        "/finance/category-budget", data={"category": "dining", "monthly_cap": "0"}, follow_redirects=True
    )
    assert b"greater than zero" in resp.data
    assert CategoryBudget.query.filter_by(user_id=user.id).count() == 0


def test_delete_category_budget_removes_row(auth_client, user):
    auth_client.post("/finance/category-budget", data={"category": "dining", "monthly_cap": "300"})
    auth_client.post("/finance/category-budget/dining/delete")

    assert CategoryBudget.query.filter_by(user_id=user.id, category="dining").count() == 0


def test_deleting_a_budget_that_was_never_set_is_a_no_op(auth_client, user):
    resp = auth_client.post("/finance/category-budget/dining/delete", follow_redirects=True)
    assert resp.status_code == 200


def test_category_budget_requires_login(client):
    resp = client.post("/finance/category-budget", data={"category": "dining", "monthly_cap": "300"}, follow_redirects=False)
    assert resp.status_code in (302, 401)


def test_set_category_budget_ajax_returns_json_on_success(auth_client, user):
    resp = auth_client.post(
        "/finance/category-budget",
        data={"category": "dining", "monthly_cap": "300"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["message"]
    row = CategoryBudget.query.filter_by(user_id=user.id, category="dining").first()
    assert row is not None
    assert row.monthly_cap == Decimal("300")


def test_set_category_budget_ajax_returns_json_error_for_invalid_category(auth_client, user):
    resp = auth_client.post(
        "/finance/category-budget",
        data={"category": "income", "monthly_cap": "300"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["message"]
    assert CategoryBudget.query.filter_by(user_id=user.id).count() == 0


def test_finance_page_shows_over_tier_for_category(auth_client, user):
    _add_expense(user, "80", category="dining")
    auth_client.post("/finance/category-budget", data={"category": "dining", "monthly_cap": "50"})

    resp = auth_client.get("/finance")
    assert b"Dining" in resp.data
    assert 'data-money-value="80.0"' in resp.get_data(as_text=True)


def test_category_budget_only_counts_its_own_category(auth_client, user):
    _add_expense(user, "500", category="groceries")
    _add_expense(user, "10", category="dining")
    auth_client.post("/finance/category-budget", data={"category": "dining", "monthly_cap": "50"})

    resp = auth_client.get("/finance")
    body = resp.get_data(as_text=True)
    assert 'data-money-value="10.0"' in body
    assert 'data-money-value="500.0"' not in body


def test_category_budgets_scoped_to_current_user_only(auth_client, user):
    other = make_user(username="bob-budgets", password="password123")
    db.session.add(CategoryBudget(user_id=other.id, category="dining", monthly_cap=Decimal("100")))
    db.session.commit()

    resp = auth_client.get("/finance")
    # "Dining" itself still legitimately appears as an option in the "add
    # a category budget" dropdown (the current user hasn't budgeted it) —
    # the real leak to guard against is the *other* user's budget row
    # rendering as an existing entry, which only happens via this delete
    # route's URL.
    assert b"/finance/category-budget/dining/delete" not in resp.data
    assert CategoryBudget.query.filter_by(user_id=other.id).count() == 1
