"""The redesigned web pages (2026-10): Today, Activity, Plan and Wealth
render the same dicts as the native app's API (services/screens.py), and
the shell they share."""

from datetime import timedelta
from decimal import Decimal

from artha.extensions import db
from artha.models import Transaction
from artha.models.budget import Budget
from artha.utils import format_money
from tests.conftest import current_period_timestamp

AJAX = {"X-Requested-With": "XMLHttpRequest"}


def _add(user, description, amount, t_type="expense", when=None, **extra):
    tx = Transaction(
        description=description,
        amount=Decimal(amount),
        type=t_type,
        user_id=user.id,
        timestamp=when or current_period_timestamp(),
        **extra,
    )
    db.session.add(tx)
    db.session.commit()
    return tx


# --- format_money ------------------------------------------------------------

def test_format_money_matches_each_currencys_own_style():
    assert format_money("1284.5", "USD") == "$1,284.50"
    assert format_money("1284.5", "USD", whole=True) == "$1,285"
    assert format_money("-12", "GBP") == "-£12.00"
    assert format_money("1234567.89", "BDT") == "৳12,34,567.89"
    assert format_money("1284.5", "EUR") == "1.284,50 €"
    assert format_money(None, "USD") == ""


# --- shell ---------------------------------------------------------------------

def test_navigation_is_today_activity_plan_wealth_then_more(auth_client):
    body = auth_client.get("/").get_data(as_text=True)
    nav = body.split('aria-label="Main navigation"', 1)[1].split("</nav>", 1)[0]
    order = [nav.index(f"<span>{label}</span>") for label in ("Today", "Activity", "Plan", "Wealth")]
    assert order == sorted(order)
    assert nav.index("<span>Wealth</span>") < nav.index(">More<") < nav.index("<span>Reports</span>")


def test_every_app_page_carries_the_add_sheet(auth_client):
    for path in ("/", "/activity", "/plan", "/accounts/", "/notes"):
        body = auth_client.get(path).get_data(as_text=True)
        assert 'id="add-sheet"' in body, path


def test_theme_follows_the_device_unless_toggled(auth_client):
    body = auth_client.get("/").get_data(as_text=True)
    assert 'localStorage.getItem("theme-choice")' in body
    assert 'localStorage.getItem("theme")' not in body


# --- Today ---------------------------------------------------------------------

def test_today_without_a_budget_leads_with_spending(auth_client, user):
    _add(user, "Groceries run", "40")
    body = auth_client.get("/").get_data(as_text=True)
    assert 'data-default="spent"' in body
    assert "Set a budget" in body
    assert "Groceries run" in body  # Recent


def test_today_hides_bills_dated_later_this_month_from_recent(auth_client, user):
    later = current_period_timestamp() + timedelta(days=1)
    if later.month != current_period_timestamp().month:
        return  # last day of the month: nothing can be later this month
    _add(user, "Posted ahead", "10", when=later)
    body = auth_client.get("/").get_data(as_text=True)
    recent = body.split(">Recent<", 1)[1]
    assert "Posted ahead" not in recent


def test_today_review_card_links_to_activity(auth_client, user):
    _add(user, "Shell", "30", import_source="apple_pay", needs_review=True)
    body = auth_client.get("/").get_data(as_text=True)
    assert "1 purchase to review" in body
    assert "/activity?review=1" in body


# --- Activity ------------------------------------------------------------------

def test_activity_groups_by_day_with_day_totals(auth_client, user):
    _add(user, "Coffee", "4.50")
    _add(user, "Lunch", "12.25")
    _add(user, "Paycheck", "1000", t_type="income")
    body = auth_client.get("/activity").get_data(as_text=True)
    assert body.count('class="day"') == 1
    assert "$16.75" in body  # the day's spending, income left out
    assert "Coffee" in body and "Lunch" in body and "Paycheck" in body


def test_activity_filters_by_type_and_search(auth_client, user):
    _add(user, "Espresso bar", "4.50")
    _add(user, "Salary deposit", "1000", t_type="income")
    body = auth_client.get("/activity?type=income").get_data(as_text=True)
    assert "Salary deposit" in body and "Espresso bar" not in body
    body = auth_client.get("/activity?q=espr").get_data(as_text=True)
    assert "Espresso bar" in body and "Salary deposit" not in body
    assert "Matching &ldquo;espr&rdquo;" in body


def test_activity_shows_a_foreign_purchase_in_its_own_currency(auth_client, user):
    _add(user, "London cab", "20.00", currency="GBP", usd_value=Decimal("25.00"))
    body = auth_client.get("/activity").get_data(as_text=True)
    assert "£20.00" in body
    assert "about $25.00" in body


def test_activity_ignores_a_malformed_month(auth_client, user):
    _add(user, "Coffee", "4.50")
    resp = auth_client.get("/activity?month=October")
    assert resp.status_code == 200
    assert "Coffee" in resp.get_data(as_text=True)


def test_activity_partial_returns_only_rows(auth_client, user):
    _add(user, "Coffee", "4.50")
    body = auth_client.get("/activity?partial=1").get_data(as_text=True)
    assert "Coffee" in body
    assert "<html" not in body


def test_activity_reviewed_clears_only_your_own(auth_client, user, app):
    mine = _add(user, "Shell", "30", needs_review=True)
    resp = auth_client.post("/activity/reviewed", json={"ids": [mine.id, 99999]}, headers=AJAX)
    assert resp.status_code == 200
    assert resp.get_json() == {"to_review": 0}
    db.session.refresh(mine)
    assert mine.needs_review is False


def test_activity_reviewed_rejects_a_bad_body(auth_client):
    resp = auth_client.post("/activity/reviewed", json={"ids": "all"}, headers=AJAX)
    assert resp.status_code == 400


# --- Plan ----------------------------------------------------------------------

def test_plan_splits_the_budget_into_bills_everyday_and_left(auth_client, user):
    db.session.add(Budget(user_id=user.id, monthly_cap=Decimal("2000"), currency="USD"))
    db.session.commit()
    _add(user, "Groceries", "300")
    body = auth_client.get("/plan").get_data(as_text=True)
    assert "$2,000" in body
    assert "Everyday spending" in body
    assert "$1,700" in body  # left to spend


def test_plan_without_a_budget_offers_to_set_one(auth_client, user):
    body = auth_client.get("/plan").get_data(as_text=True)
    assert "Set budget" in body


def test_removing_a_category_limit_answers_json_for_the_plan_page(auth_client, user):
    auth_client.post("/finance/category-budget", data={"category": "dining", "monthly_cap": "200"}, headers=AJAX)
    resp = auth_client.post("/finance/category-budget/dining/delete", headers=AJAX)
    assert resp.status_code == 200
    resp = auth_client.post("/finance/category-budget/dining/delete", headers=AJAX)
    assert resp.status_code == 404


# --- Wealth --------------------------------------------------------------------

def test_wealth_without_accounts_invites_the_first(auth_client):
    body = auth_client.get("/accounts/").get_data(as_text=True)
    assert "Add your first account" in body


# --- Copy ----------------------------------------------------------------------

def test_redesigned_pages_have_no_em_dashes(auth_client, user):
    _add(user, "Coffee", "4.50")
    for path in ("/", "/activity", "/plan", "/accounts/"):
        body = auth_client.get(path).get_data(as_text=True)
        visible = body.split("<body", 1)[1]
        # Strip HTML comments and scripts, which may carry em dashes in
        # developer notes (exempt by the project's own rule).
        import re
        visible = re.sub(r"<!--.*?-->", "", visible, flags=re.S)
        visible = re.sub(r"<script.*?</script>", "", visible, flags=re.S)
        visible = re.sub(r"<style.*?</style>", "", visible, flags=re.S)
        assert "—" not in visible, path
