"""The native app's JSON API (artha/blueprints/api): bearer-token sign-in,
Today, and transactions. See that module's docstring for the design."""

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pyotp
import pytest

from artha.extensions import db
from artha.models import ApiToken, ExchangeRate, Transaction, User
from artha.models.account import Account, NetWorthSnapshot
from artha.models.budget import Budget
from artha.services.month_snapshot import build_month_snapshot
from artha.utils import user_today

from .conftest import current_period_timestamp, make_user

RATES = {"USD": 1, "GBP": 0.80, "EUR": 0.92, "BDT": 120.0, "CAD": 1.35, "AUD": 1.50}


def _sign_in(client, username="alice", password="password123", **extra):
    return client.post("/api/v1/sessions", json={"username": username, "password": password, **extra})


def _token(client, user, password="password123"):
    resp = _sign_in(client, user.username, password)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _add(user, description, amount, t_type="expense", when=None, **extra):
    tx = Transaction(
        description=description, amount=Decimal(amount), type=t_type, user_id=user.id,
        timestamp=when or current_period_timestamp(), currency=extra.pop("currency", "USD"),
        **extra,
    )
    if tx.usd_value is None:
        tx.usd_value = tx.amount
    db.session.add(tx)
    db.session.commit()
    return tx


# ---------------------------------------------------------------------------
# Signing in and out
# ---------------------------------------------------------------------------

def test_sign_in_returns_a_token_and_stores_only_its_hash(client, user):
    resp = _sign_in(client, device_name="Test iPhone")
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["token_type"] == "Bearer"
    assert body["user"]["username"] == "alice"
    assert body["deletion_canceled"] is False

    stored = ApiToken.query.one()
    assert stored.token_hash == ApiToken.hash_token(body["token"])
    assert stored.token_hash != body["token"]
    assert stored.device_name == "Test iPhone"


def test_wrong_password_and_unknown_user_are_rejected_the_same_way(client, user):
    for resp in (_sign_in(client, password="nope"), _sign_in(client, username="nobody")):
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "invalid_credentials"
    assert ApiToken.query.count() == 0


def test_two_factor_account_needs_a_valid_code(client):
    secret = pyotp.random_base32()
    make_user(username="secure", otp_enabled=True, otp_secret=secret)

    missing = _sign_in(client, "secure")
    assert missing.status_code == 401
    assert missing.get_json()["error"] == "two_factor_required"

    wrong = _sign_in(client, "secure", code="000000")
    assert wrong.status_code == 401
    assert wrong.get_json()["error"] == "invalid_code"
    assert ApiToken.query.count() == 0

    ok = _sign_in(client, "secure", code=pyotp.TOTP(secret).now())
    assert ok.status_code == 201


def test_requests_without_a_valid_token_get_json_401(client, user):
    for headers in ({}, {"Authorization": "Bearer not-a-real-token"}, {"Authorization": "Basic abc"}):
        resp = client.get("/api/v1/me", headers=headers)
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "unauthorized"


def test_a_web_session_cookie_does_not_authenticate_the_api(auth_client):
    resp = auth_client.get("/api/v1/me")
    assert resp.status_code == 401


def test_me_returns_the_signed_in_user(client, user):
    token = _token(client, user)
    resp = client.get("/api/v1/me", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.get_json()["user"]["currency"] == "USD"


def test_signing_out_revokes_the_token(client, user):
    token = _token(client, user)
    assert client.delete("/api/v1/sessions/current", headers=_auth(token)).status_code == 204
    assert client.get("/api/v1/me", headers=_auth(token)).status_code == 401
    assert ApiToken.query.count() == 0


def test_changing_password_signs_the_app_out_everywhere(client, user):
    token = _token(client, user)
    client.post("/login", data={"username": "alice", "password": "password123"})
    client.post("/change_password", data={
        "current_password": "password123", "new_password": "different456", "confirm_password": "different456",
    })
    assert client.get("/api/v1/me", headers=_auth(token)).status_code == 401


def test_requesting_account_deletion_signs_the_app_out_and_signing_in_cancels_it(client, user):
    token = _token(client, user)
    client.post("/login", data={"username": "alice", "password": "password123"})
    client.post("/account/delete", data={"password": "password123", "confirm_username": "alice"})
    assert db.session.get(User, user.id).deleted_at is not None
    assert client.get("/api/v1/me", headers=_auth(token)).status_code == 401

    resp = _sign_in(client)
    assert resp.status_code == 201
    assert resp.get_json()["deletion_canceled"] is True
    assert db.session.get(User, user.id).deleted_at is None


def test_deleting_a_user_deletes_their_tokens(client, user):
    _token(client, user)
    db.session.delete(db.session.get(User, user.id))
    db.session.commit()
    assert ApiToken.query.count() == 0


def test_sign_in_is_rate_limited_with_a_json_429(rate_limited_client):
    make_user()
    statuses = [_sign_in(rate_limited_client, password="wrong").status_code for _ in range(6)]
    assert statuses[:5] == [401] * 5
    last = _sign_in(rate_limited_client, password="wrong")
    assert last.status_code == 429
    assert last.get_json()["error"] == "rate_limited"


def test_unknown_api_paths_and_methods_answer_in_json(client):
    resp = client.get("/api/v1/nope")
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "not_found"

    resp = client.put("/api/v1/me")
    assert resp.status_code == 405
    assert resp.get_json()["error"] == "method_not_allowed"


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

def test_add_transaction_runs_the_web_rules(client, user):
    token = _token(client, user)
    resp = client.post("/api/v1/transactions", headers=_auth(token), json={
        "description": "Trader Joe's", "amount": "48.20", "type": "expense",
    })
    assert resp.status_code == 201
    tx = resp.get_json()["transaction"]
    assert tx["amount"] == "48.20"
    assert tx["currency"] == "USD"
    assert tx["category"] == "groceries"  # same keyword categorizer as the web form
    assert tx["date"] == date.today().isoformat()
    assert tx["display_amount"] is None

    saved = db.session.get(Transaction, tx["id"])
    assert saved.user_id == user.id
    assert saved.import_source == "manual"
    assert saved.usd_value == Decimal("48.20")


def test_add_transaction_needs_no_csrf_token_while_the_web_form_still_does(client, user, app):
    token = _token(client, user)
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        api = client.post("/api/v1/transactions", headers=_auth(token), json={
            "description": "Lyft", "amount": 8.45, "type": "expense",
        })
        assert api.status_code == 201

        client.post("/login", data={"username": "alice", "password": "password123"})
        web = client.post("/add_transaction", data={"description": "Lyft", "amount": "8.45", "type": "expense"},
                          headers={"X-Requested-With": "XMLHttpRequest"})
        assert web.status_code == 400
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


@pytest.mark.parametrize("payload, message", [
    ({"amount": "5", "type": "expense"}, "Description is required."),
    ({"description": "x", "amount": "abc", "type": "expense"}, "Invalid amount format."),
    ({"description": "x", "amount": "NaN", "type": "expense"}, "Invalid amount format."),
    ({"description": "x", "amount": "-1", "type": "expense"}, "Amount must be non-negative."),
    ({"description": "x", "amount": "5", "type": "gift"}, "Invalid transaction type."),
])
def test_add_transaction_validation_errors_are_400s(client, user, payload, message):
    token = _token(client, user)
    resp = client.post("/api/v1/transactions", headers=_auth(token), json=payload)
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "invalid_transaction", "message": message}
    assert Transaction.query.count() == 0


def test_add_transaction_rejects_a_non_object_body(client, user):
    token = _token(client, user)
    resp = client.post("/api/v1/transactions", headers=_auth(token), data="[1, 2]", content_type="application/json")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_request"


def test_json_booleans_and_weekly_recurrence(client, user):
    token = _token(client, user)
    with patch("artha.blueprints.finance.routes._fill_uncategorized_via_ai"):
        one_off = client.post("/api/v1/transactions", headers=_auth(token), json={
            "description": "Gift", "amount": "20", "type": "expense", "is_recurring": False,
            "recurrence_interval": "weekly",
        }).get_json()["transaction"]
        weekly = client.post("/api/v1/transactions", headers=_auth(token), json={
            "description": "Cleaner", "amount": "60", "type": "expense", "is_recurring": True,
            "recurrence_interval": "weekly",
        }).get_json()["transaction"]
    assert one_off["is_recurring"] is False
    assert one_off["recurrence_interval"] is None
    assert weekly["is_recurring"] is True
    assert weekly["recurrence_interval"] == "weekly"


def test_foreign_currency_transaction_keeps_its_own_amount_and_adds_a_converted_one(client, user):
    db.session.add(ExchangeRate(base="USD", rates_json=json.dumps(RATES), source="open-er-api"))
    db.session.commit()
    token = _token(client, user)
    resp = client.post("/api/v1/transactions", headers=_auth(token), json={
        "description": "Family, Dhaka", "amount": "12000", "type": "expense", "currency": "BDT", "category": "other",
    })
    tx = resp.get_json()["transaction"]
    assert tx["amount"] == "12000.00"
    assert tx["currency"] == "BDT"
    assert tx["display_amount"] == "100.00"  # 12,000 BDT at 120 per USD


def test_list_transactions_is_newest_first_paged_and_only_yours(client, user):
    other = make_user(username="bob")
    _add(other, "Not yours", "1.00")
    base = current_period_timestamp()
    for i in range(3):
        _add(user, f"Item {i}", "1.00", when=base - timedelta(days=i))
    token = _token(client, user)

    first = client.get("/api/v1/transactions?limit=2", headers=_auth(token)).get_json()
    assert [t["description"] for t in first["transactions"]] == ["Item 0", "Item 1"]
    assert first["today"] == user_today(user).isoformat()
    assert first["next_offset"] == 2

    second = client.get("/api/v1/transactions?limit=2&offset=2", headers=_auth(token)).get_json()
    assert [t["description"] for t in second["transactions"]] == ["Item 2"]
    assert second["next_offset"] is None

    assert client.get("/api/v1/transactions?limit=abc", headers=_auth(token)).status_code == 400


def test_list_transactions_searches_descriptions_and_filters_by_type(client, user):
    _add(user, "Blue Bottle Coffee", "6.75")
    _add(user, "BLUE apron", "60.00")
    _add(user, "Salary", "3000.00", t_type="income")
    _add(user, "Paid 50% upfront", "10.00")
    _add(user, "Paid 500 upfront", "10.00")
    token = _token(client, user)

    def descriptions(query):
        resp = client.get(f"/api/v1/transactions?{query}", headers=_auth(token))
        assert resp.status_code == 200, resp.get_json()
        return sorted(t["description"] for t in resp.get_json()["transactions"])

    assert descriptions("q=blue") == ["BLUE apron", "Blue Bottle Coffee"]
    assert descriptions("type=income") == ["Salary"]
    assert descriptions("q=blue&type=income") == []
    # % is literal text in a search, not a wildcard.
    assert descriptions("q=50%25") == ["Paid 50% upfront"]
    assert client.get("/api/v1/transactions?type=transfer", headers=_auth(token)).status_code == 400


def test_list_transactions_filters_by_month_and_category(client, user):
    this_month = current_period_timestamp()
    last_month = this_month.replace(day=1) - timedelta(days=3)
    _add(user, "Chipotle", "12.00", category="dining", when=this_month)
    _add(user, "Whole Foods", "40.00", category="groceries", when=this_month)
    _add(user, "Mystery", "5.00", category=None, when=this_month)
    _add(user, "Old dinner", "30.00", category="dining", when=last_month)
    token = _token(client, user)
    month = this_month.strftime("%Y-%m")

    def descriptions(query):
        resp = client.get(f"/api/v1/transactions?{query}", headers=_auth(token))
        assert resp.status_code == 200, resp.get_json()
        return sorted(t["description"] for t in resp.get_json()["transactions"])

    assert descriptions(f"month={month}") == ["Chipotle", "Mystery", "Whole Foods"]
    assert descriptions("category=dining") == ["Chipotle", "Old dinner"]
    assert descriptions(f"month={month}&category=dining") == ["Chipotle"]
    assert descriptions(f"month={month}&category=groceries,none") == ["Mystery", "Whole Foods"]
    assert client.get("/api/v1/transactions?month=October", headers=_auth(token)).status_code == 400


def test_edit_transaction_runs_the_web_rules_and_remembers_the_category(client, user):
    tx = _add(user, "Blue Bottle Coffee", "6.75", category="other")
    token = _token(client, user)

    resp = client.patch(f"/api/v1/transactions/{tx.id}", headers=_auth(token),
                        json={"amount": "7.25", "category": "dining", "date": "2026-10-03"})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()["transaction"]
    assert body["amount"] == "7.25"
    assert body["category"] == "dining"
    assert body["category_label"] == "Dining"
    assert body["date"] == "2026-10-03"
    assert body["description"] == "Blue Bottle Coffee"

    db.session.refresh(tx)
    assert tx.usd_value == Decimal("7.25")

    # The correction is remembered: the next Blue Bottle add files itself.
    added = client.post("/api/v1/transactions", headers=_auth(token),
                        json={"description": "Blue Bottle Coffee", "amount": "5", "type": "expense"})
    assert added.get_json()["transaction"]["category"] == "dining"


@pytest.mark.parametrize("payload, message", [
    ({"amount": "-1"}, "Amount must be non-negative."),
    ({"amount": "abc"}, "Invalid amount format."),
    ({"amount": "Infinity"}, "Invalid amount format."),
    ({"type": "transfer"}, "Invalid transaction type."),
])
def test_edit_transaction_validation_errors_are_400s_and_change_nothing(client, user, payload, message):
    tx = _add(user, "Lunch", "12.00")
    token = _token(client, user)
    resp = client.patch(f"/api/v1/transactions/{tx.id}", headers=_auth(token), json=payload)
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "invalid_transaction", "message": message}
    db.session.expire_all()
    assert db.session.get(Transaction, tx.id).amount == Decimal("12.00")


def test_someone_elses_transaction_reads_as_missing(client, user):
    other = make_user(username="bob")
    theirs = _add(other, "Not yours", "1.00")
    token = _token(client, user)

    edit = client.patch(f"/api/v1/transactions/{theirs.id}", headers=_auth(token), json={"amount": "0"})
    delete = client.delete(f"/api/v1/transactions/{theirs.id}", headers=_auth(token))
    for resp in (edit, delete):
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "not_found"
    db.session.expire_all()
    assert db.session.get(Transaction, theirs.id).amount == Decimal("1.00")


def test_delete_transaction_stops_its_recurring_series(client, user):
    base = current_period_timestamp()
    older = _add(user, "Gym", "40.00", when=base - timedelta(days=31), is_recurring=True)
    newer = _add(user, "Gym", "40.00", when=base, is_recurring=True)
    token = _token(client, user)

    resp = client.delete(f"/api/v1/transactions/{newer.id}", headers=_auth(token))
    assert resp.status_code == 204
    db.session.expire_all()
    assert db.session.get(Transaction, newer.id) is None
    assert db.session.get(Transaction, older.id).is_recurring is False


def test_categories_lists_every_category_in_order(client, user):
    token = _token(client, user)
    body = client.get("/api/v1/categories", headers=_auth(token)).get_json()
    keys = [c["key"] for c in body["categories"]]
    assert keys[0] == "income"
    assert "dining" in keys and "other" in keys
    assert {"key": "debt", "label": "Debt & loans"} in body["categories"]


# ---------------------------------------------------------------------------
# Today
# ---------------------------------------------------------------------------

def test_today_without_a_budget_has_no_safe_to_spend(client, user):
    _add(user, "Coffee", "5.00", category="dining")
    token = _token(client, user)
    body = client.get("/api/v1/today", headers=_auth(token)).get_json()
    assert body["budget"] is None
    assert body["safe_to_spend"] is None
    assert body["spent"] == "5.00"
    assert body["categories"] == [{"key": "dining", "label": "Dining", "amount": "5.00"}]
    assert body["recent"][0]["description"] == "Coffee"


def test_today_matches_the_dashboard_figures(client, user):
    db.session.add(Budget(user_id=user.id, monthly_cap=Decimal("3400"), currency="USD"))
    db.session.commit()
    _add(user, "Trader Joe's", "48.20", category="groceries")
    _add(user, "Salary", "4000.00", t_type="income", category="income")
    token = _token(client, user)

    body = client.get("/api/v1/today", headers=_auth(token)).get_json()
    snap = build_month_snapshot(db.session.get(User, user.id))

    assert body["currency"] == "USD"
    assert body["spent"] == "48.20"
    assert body["income"] == "4000.00"
    assert body["budget"]["cap"] == "3400.00"
    assert Decimal(body["safe_to_spend"]["amount"]) == Decimal(str(snap.safe_to_spend)).quantize(Decimal("0.01"))
    assert body["safe_to_spend"]["status"] == "on_pace"
    assert body["safe_to_spend"]["over_by"] == "0.00"
    assert body["month"]["days_left"] == snap.days_in_month - date.today().day + 1

    by_day = body["spending_by_day"]
    assert len(by_day) == date.today().day
    assert by_day[-1]["spent"] == "48.20"


def test_today_reports_over_budget_without_a_negative_per_day(client, user):
    db.session.add(Budget(user_id=user.id, monthly_cap=Decimal("100"), currency="USD"))
    db.session.commit()
    _add(user, "Big purchase", "250.00", category="shopping")
    token = _token(client, user)

    sts = client.get("/api/v1/today", headers=_auth(token)).get_json()["safe_to_spend"]
    assert sts["status"] == "over"
    assert sts["amount"] == "-150.00"
    assert sts["over_by"] == "150.00"
    assert sts["per_day"] == "0.00"


def test_today_recent_leaves_out_bills_dated_later_this_month(client, user):
    _add(user, "Coffee", "5.00", category="dining")
    _add(user, "Rent", "1650.00", category="housing", when=current_period_timestamp() + timedelta(days=3))
    token = _token(client, user)
    recent = client.get("/api/v1/today", headers=_auth(token)).get_json()["recent"]
    assert [t["description"] for t in recent] == ["Coffee"]


def test_today_lists_recurring_bills_coming_up(client, user):
    today = date.today()
    last_month = (today.replace(day=1) - timedelta(days=1)).replace(day=min(today.day, 28))
    _add(user, "Spotify", "11.99", category="subscriptions", is_recurring=True,
         when=datetime(last_month.year, last_month.month, last_month.day, 12, tzinfo=timezone.utc))
    token = _token(client, user)

    coming = client.get("/api/v1/today", headers=_auth(token)).get_json()["coming_up"]
    spotify = [b for b in coming if b["description"] == "Spotify"]
    assert spotify, coming
    assert spotify[0]["amount"] == "11.99"
    assert spotify[0]["currency"] == "USD"
    assert 0 <= spotify[0]["days_away"] <= 31


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def test_plan_breaks_the_budget_into_bills_everyday_and_what_is_left(client, user):
    db.session.add(Budget(user_id=user.id, monthly_cap=Decimal("3400"), currency="USD"))
    db.session.commit()
    _add(user, "Trader Joe's", "48.20", category="groceries")
    _add(user, "Rent", "1650.00", category="housing", is_recurring=True)
    token = _token(client, user)

    plan = client.get("/api/v1/plan", headers=_auth(token)).get_json()
    today = client.get("/api/v1/today", headers=_auth(token)).get_json()

    assert plan["budget"]["cap"] == "3400.00"
    breakdown = plan["breakdown"]
    assert breakdown["bills"] == "1650.00"
    assert breakdown["everyday_spent"] == "48.20"
    assert breakdown["everyday_budget"] == "1750.00"
    # Same number Today calls Safe to Spend.
    assert breakdown["left"] == today["safe_to_spend"]["amount"] == "1701.80"
    assert [(b["description"], b["status"]) for b in plan["bills"]] == [("Rent", "paid")]
    assert plan["bills_paid"] == "1650.00"
    assert plan["bills_coming"] == "0.00"


def test_plan_without_a_budget_still_lists_bills(client, user):
    _add(user, "Rent", "1650.00", category="housing", is_recurring=True)
    token = _token(client, user)
    plan = client.get("/api/v1/plan", headers=_auth(token)).get_json()
    assert plan["budget"] is None
    assert plan["breakdown"] is None
    assert plan["bills_total"] == "1650.00"
    assert plan["typical"] is None
    assert {"key": "dining", "label": "Dining"} in plan["unbudgeted_categories"]
    assert all(c["key"] != "income" for c in plan["unbudgeted_categories"])


def test_put_budget_saves_in_the_display_currency_and_zero_clears_it(client, user):
    user.preferred_currency = "GBP"
    db.session.commit()
    token = _token(client, user)

    assert client.put("/api/v1/budget", headers=_auth(token), json={"monthly_cap": "2500"}).status_code == 204
    row = Budget.query.filter_by(user_id=user.id).one()
    assert (row.monthly_cap, row.currency) == (Decimal("2500.00"), "GBP")

    bad = client.put("/api/v1/budget", headers=_auth(token), json={"monthly_cap": "-5"})
    assert bad.status_code == 400
    assert bad.get_json()["error"] == "invalid_budget"

    assert client.put("/api/v1/budget", headers=_auth(token), json={"monthly_cap": "0"}).status_code == 204
    assert client.get("/api/v1/plan", headers=_auth(token)).get_json()["budget"] is None


def test_category_limits_can_be_set_shown_and_removed(client, user):
    _add(user, "Dinner", "120.00", category="dining")
    token = _token(client, user)

    assert client.put("/api/v1/category-budgets/dining", headers=_auth(token),
                      json={"monthly_cap": "100"}).status_code == 204
    plan = client.get("/api/v1/plan", headers=_auth(token)).get_json()
    assert plan["category_budgets"] == [{
        "key": "dining", "label": "Dining", "cap": "100.00", "spent": "120.00",
        "left": "0.00", "over_by": "20.00", "percent_used": 120.0, "tier": "over",
        "rollover": False, "base_cap": "100.00", "carried": None,
    }]
    assert all(c["key"] != "dining" for c in plan["unbudgeted_categories"])

    for category, payload in (("income", {"monthly_cap": "100"}), ("dining", {"monthly_cap": "0"})):
        resp = client.put(f"/api/v1/category-budgets/{category}", headers=_auth(token), json=payload)
        assert resp.status_code == 400

    assert client.delete("/api/v1/category-budgets/dining", headers=_auth(token)).status_code == 204
    assert client.delete("/api/v1/category-budgets/dining", headers=_auth(token)).status_code == 404
    assert client.get("/api/v1/plan", headers=_auth(token)).get_json()["category_budgets"] == []


# ---------------------------------------------------------------------------
# Wealth
# ---------------------------------------------------------------------------

def test_wealth_without_accounts_is_empty_but_lists_types(client, user):
    token = _token(client, user)
    body = client.get("/api/v1/wealth", headers=_auth(token)).get_json()
    assert body["net_worth"] is None
    assert body["accounts"] == []
    assert body["history"] == []
    assert {"key": "credit_card", "label": "Credit Card", "is_liability": True, "group": "debts"} in body["account_types"]
    assert "BDT" in body["currencies"]


def test_accounts_add_up_to_net_worth_in_the_display_currency(client, user):
    db.session.add(ExchangeRate(base="USD", rates_json=json.dumps(RATES), source="open-er-api"))
    db.session.commit()
    token = _token(client, user)

    def add(**fields):
        resp = client.post("/api/v1/accounts", headers=_auth(token), json=fields)
        assert resp.status_code == 201, resp.get_json()
        return resp.get_json()["account"]

    checking = add(name="Chase", account_type="checking", current_balance="5000")
    add(name="Visa", account_type="credit_card", current_balance="1200")
    family = add(name="Dhaka savings", account_type="savings", currency="BDT", current_balance="120000")

    assert checking["currency"] == "USD"
    assert checking["group"] == "cash"
    assert checking["display_balance"] is None
    assert family["balance"] == "120000.00"
    assert family["display_balance"] == "1000.00"  # 120,000 BDT at 120 per USD

    body = client.get("/api/v1/wealth", headers=_auth(token)).get_json()
    assert body["assets"] == "6000.00"
    assert body["liabilities"] == "1200.00"
    assert body["net_worth"] == "4800.00"
    assert body["history"][-1] == {"date": user_today(user).isoformat(), "net_worth": "4800.00"}
    # Matches what Today shows as net worth.
    today = client.get("/api/v1/today", headers=_auth(token)).get_json()
    assert today["net_worth"]["amount"] == "4800.00"


def test_wealth_history_keeps_past_days_and_ends_with_today(client, user):
    token = _token(client, user)
    client.post("/api/v1/accounts", headers=_auth(token),
                json={"name": "Chase", "account_type": "checking", "current_balance": "1000"})
    yesterday = user_today(user) - timedelta(days=1)
    db.session.add(NetWorthSnapshot(user_id=user.id, snapshot_date=yesterday,
                                    total_assets_usd=Decimal("800"), total_liabilities_usd=Decimal("0")))
    db.session.commit()

    history = client.get("/api/v1/wealth", headers=_auth(token)).get_json()["history"]
    assert history == [
        {"date": yesterday.isoformat(), "net_worth": "800.00"},
        {"date": user_today(user).isoformat(), "net_worth": "1000.00"},
    ]


def test_editing_an_account_changes_only_what_was_sent(client, user):
    token = _token(client, user)
    created = client.post("/api/v1/accounts", headers=_auth(token),
                          json={"name": "Chase", "account_type": "checking", "current_balance": "1000"}).get_json()["account"]
    account = db.session.get(Account, created["id"])
    long_ago = datetime(2026, 1, 1, tzinfo=timezone.utc)
    account.balance_updated_at = long_ago
    db.session.commit()

    renamed = client.patch(f"/api/v1/accounts/{created['id']}", headers=_auth(token), json={"name": "Chase Total"})
    assert renamed.status_code == 200
    body = renamed.get_json()["account"]
    assert (body["name"], body["balance"], body["updated"]) == ("Chase Total", "1000.00", "2026-01-01")

    # Sending the balance, even unchanged, confirms it as up to date.
    confirmed = client.patch(f"/api/v1/accounts/{created['id']}", headers=_auth(token),
                             json={"current_balance": "1000"}).get_json()["account"]
    assert confirmed["days_since_update"] == 0


@pytest.mark.parametrize("payload, message", [
    ({"name": "", "account_type": "checking", "current_balance": "1"}, "Account name is required."),
    ({"name": "X", "account_type": "boat", "current_balance": "1"}, "Unknown account type."),
    ({"name": "X", "account_type": "checking", "currency": "XTS", "current_balance": "1"}, "Unsupported currency."),
    ({"name": "X", "account_type": "loan", "current_balance": "-5"},
     "Balance must be zero or positive. A liability's balance is what's owed, not a negative number."),
    ({"name": "X", "account_type": "checking", "current_balance": "NaN"}, "Balance must be a valid number."),
])
def test_account_validation_errors_are_400s(client, user, payload, message):
    token = _token(client, user)
    resp = client.post("/api/v1/accounts", headers=_auth(token), json=payload)
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "invalid_account", "message": message}
    assert Account.query.count() == 0


def test_someone_elses_account_reads_as_missing_and_delete_works(client, user):
    other = make_user(username="bob")
    theirs = Account(user_id=other.id, name="Bob's", account_type="checking", currency="USD", current_balance=Decimal("9"))
    db.session.add(theirs)
    db.session.commit()
    token = _token(client, user)

    assert client.patch(f"/api/v1/accounts/{theirs.id}", headers=_auth(token), json={"name": "Mine"}).status_code == 404
    assert client.delete(f"/api/v1/accounts/{theirs.id}", headers=_auth(token)).status_code == 404

    mine = client.post("/api/v1/accounts", headers=_auth(token),
                       json={"name": "Chase", "account_type": "checking", "current_balance": "10"}).get_json()["account"]
    assert client.delete(f"/api/v1/accounts/{mine['id']}", headers=_auth(token)).status_code == 204
    assert client.get("/api/v1/wealth", headers=_auth(token)).get_json()["accounts"] == []


# ---------------------------------------------------------------------------
# Apple Pay logging
# ---------------------------------------------------------------------------

def test_apple_pay_purchase_is_marked_and_categorized_like_any_other(client, user):
    token = _token(client, user)
    resp = client.post("/api/v1/transactions", headers=_auth(token), json={
        "description": "Blue Bottle Coffee", "amount": "6.75", "type": "expense",
        "currency": "USD", "source": "apple_pay",
    })
    assert resp.status_code == 201
    body = resp.get_json()["transaction"]
    assert body["source"] == "apple_pay"
    assert body["category"] == "dining"
    assert Transaction.query.one().import_source == "apple_pay"

    typed = client.post("/api/v1/transactions", headers=_auth(token), json={
        "description": "Lunch", "amount": "12", "type": "expense", "source": "something_else",
    }).get_json()["transaction"]
    assert typed["source"] == "manual"


def test_an_unsupported_currency_is_refused_instead_of_relabeled(client, user):
    token = _token(client, user)
    resp = client.post("/api/v1/transactions", headers=_auth(token), json={
        "description": "Test purchase", "amount": "1500", "type": "expense", "currency": "xts", "source": "apple_pay",
    })
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "unsupported_currency"
    assert "XTS" in body["message"]
    assert Transaction.query.count() == 0


# ---------------------------------------------------------------------------
# Signing up and deleting an account from the app
# ---------------------------------------------------------------------------

def _sign_up(client, **overrides):
    body = {"email": "new@example.com", "username": "newperson", "password": "a-strong-pass",
            "first_name": "Nia", "currency": "GBP", "timezone": "Europe/London", "device_name": "iPhone"}
    body.update(overrides)
    return client.post("/api/v1/users", json=body)


def test_sign_up_creates_the_account_and_signs_this_device_in(client):
    resp = _sign_up(client)
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    assert body["token_type"] == "Bearer"
    assert body["user"]["username"] == "newperson"
    assert body["user"]["currency"] == "GBP"
    assert body["user"]["timezone"] == "Europe/London"

    user = User.query.filter_by(username="newperson").one()
    assert user.email == "new@example.com"
    assert user.first_name == "Nia"
    assert user.check_password("a-strong-pass")
    assert client.get("/api/v1/me", headers=_auth(body["token"])).status_code == 200


def test_sign_up_ignores_an_unknown_currency_or_timezone(client):
    body = _sign_up(client, currency="XTS", timezone="Mars/Olympus").get_json()
    assert body["user"]["currency"] == "USD"
    assert body["user"]["timezone"] is None


@pytest.mark.parametrize("overrides, message", [
    ({"email": ""}, "Email, username, and password are required."),
    ({"email": "nope"}, "That doesn't look like a valid email address."),
    ({"password": "short"}, "Password must be at least 8 characters."),
    ({"username": "alice"}, "Username already exists."),
    ({"email": "ALICE@example.com"}, "Email already exists."),
])
def test_sign_up_follows_the_web_rules(client, user, overrides, message):
    user.email = "alice@example.com"
    db.session.commit()
    resp = _sign_up(client, **overrides)
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "invalid_account", "message": message}


def test_sign_up_is_rate_limited(rate_limited_client):
    statuses = [_sign_up(rate_limited_client, username=f"p{i}", email=f"p{i}@example.com").status_code
                for i in range(6)]
    assert statuses[:5] == [201] * 5
    assert statuses[5] == 429


def test_deleting_from_the_app_needs_the_password_and_signs_every_device_out(client, user):
    phone = _token(client, user)
    tablet = _token(client, user)

    wrong = client.delete("/api/v1/me", headers=_auth(phone), json={"password": "nope"})
    assert wrong.status_code == 400
    assert wrong.get_json()["error"] == "invalid_password"
    assert db.session.get(User, user.id).deleted_at is None

    with patch("artha.blueprints.auth.routes.send_account_deletion_email") as email:
        resp = client.delete("/api/v1/me", headers=_auth(phone), json={"password": "password123"})
    assert resp.status_code == 202
    assert resp.get_json()["purge_date"]
    email.assert_called_once()
    assert db.session.get(User, user.id).deleted_at is not None
    for token in (phone, tablet):
        assert client.get("/api/v1/me", headers=_auth(token)).status_code == 401

    # Signing back in during the grace window still cancels it.
    assert _sign_in(client).get_json()["deletion_canceled"] is True


def test_the_only_admin_cant_delete_from_the_app_either(client, user):
    user.is_admin = True
    db.session.commit()
    token = _token(client, user)
    resp = client.delete("/api/v1/me", headers=_auth(token), json={"password": "password123"})
    assert resp.status_code == 409
    assert db.session.get(User, user.id).deleted_at is None


def test_apple_app_site_association_links_only_passwords_to_the_app(client):
    resp = client.get("/.well-known/apple-app-site-association")
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    assert resp.get_json() == {"webcredentials": {"apps": ["7GGTC43CG8.studio.anwarcreative.artha"]}}


# ---------------------------------------------------------------------------
# Reviewing purchases that logged themselves
# ---------------------------------------------------------------------------

def test_apple_pay_purchases_wait_for_review_and_typed_ones_dont(client, user):
    token = _token(client, user)
    tapped = client.post("/api/v1/transactions", headers=_auth(token), json={
        "description": "Blue Bottle Coffee", "amount": "6.75", "type": "expense", "source": "apple_pay"})
    typed = client.post("/api/v1/transactions", headers=_auth(token), json={
        "description": "Farmers market", "amount": "20.00", "type": "expense"})
    assert tapped.get_json()["transaction"]["needs_review"] is True
    assert typed.get_json()["transaction"]["needs_review"] is False

    assert client.get("/api/v1/today", headers=_auth(token)).get_json()["to_review"] == 1
    listed = client.get("/api/v1/transactions?review=1", headers=_auth(token)).get_json()["transactions"]
    assert [t["description"] for t in listed] == ["Blue Bottle Coffee"]


def test_marking_reviewed_clears_only_your_own(client, user):
    other = make_user(username="bob")
    mine = _add(user, "Uber", "18.40", needs_review=True)
    also_mine = _add(user, "Lyft", "14.75", needs_review=True)
    theirs = _add(other, "Taxi", "9.00", needs_review=True)
    token = _token(client, user)

    resp = client.post("/api/v1/transactions/reviewed", headers=_auth(token), json={"ids": [mine.id, theirs.id]})
    assert resp.status_code == 200
    assert resp.get_json() == {"to_review": 1}
    assert db.session.get(Transaction, mine.id).needs_review is False
    assert db.session.get(Transaction, also_mine.id).needs_review is True
    assert db.session.get(Transaction, theirs.id).needs_review is True

    bad = client.post("/api/v1/transactions/reviewed", headers=_auth(token), json={"ids": "all"})
    assert bad.status_code == 400


def test_changing_a_purchase_counts_as_reviewing_it(client, user):
    tx = _add(user, "Shell", "48.90", category="shopping", needs_review=True)
    token = _token(client, user)
    resp = client.patch(f"/api/v1/transactions/{tx.id}", headers=_auth(token), json={"category": "transport"})
    assert resp.status_code == 200
    assert resp.get_json()["transaction"]["needs_review"] is False


# ---------------------------------------------------------------------------
# Month by month, and whether Apple Pay logging is working
# ---------------------------------------------------------------------------

def test_history_buckets_months_and_skips_empty_ones_before_the_first(client, user):
    today = user_today(user)
    this_month = datetime(today.year, today.month, 1, 12, tzinfo=timezone.utc)
    last_month = (this_month - timedelta(days=1)).replace(day=10)
    # The 3rd, so this first month counts as a full one (see the partial test).
    two_back = (last_month.replace(day=1) - timedelta(days=1)).replace(day=3)
    _add(user, "Rent", "1000.00", category="housing", when=two_back)
    _add(user, "Paycheck", "3000.00", t_type="income", when=two_back)
    _add(user, "Groceries", "200.00", category="groceries", when=last_month)
    _add(user, "Rent", "1000.00", category="housing", when=last_month)
    _add(user, "Coffee", "5.00", category="dining", when=this_month)
    token = _token(client, user)

    body = client.get("/api/v1/history?months=6", headers=_auth(token)).get_json()
    months = body["months"]
    # Six months asked for, but the account only goes back three.
    assert [m["month"] for m in months] == [
        two_back.strftime("%Y-%m"), last_month.strftime("%Y-%m"), this_month.strftime("%Y-%m")]
    assert months[0]["spent"] == "1000.00" and months[0]["income"] == "3000.00"
    assert months[1]["spent"] == "1200.00"
    assert [c["key"] for c in months[1]["categories"]] == ["housing", "groceries"]
    assert months[-1]["is_current"] is True and months[0]["is_current"] is False
    # This month is still going, so the average is the two finished ones.
    assert body["average_spent"] == "1100.00"
    assert client.get("/api/v1/history?months=lots", headers=_auth(token)).status_code == 400


def test_history_leaves_a_partial_first_month_out_of_the_usual(client, user):
    today = user_today(user)
    this_month = datetime(today.year, today.month, 1, 12, tzinfo=timezone.utc)
    last_month = (this_month - timedelta(days=1)).replace(day=3)
    two_back = (last_month.replace(day=1) - timedelta(days=1)).replace(day=27)
    _add(user, "Chipotle", "13.00", category="dining", when=two_back)
    _add(user, "Rent", "2000.00", category="housing", when=last_month)
    token = _token(client, user)

    body = client.get("/api/v1/history", headers=_auth(token)).get_json()
    first = body["months"][0]
    assert first["started"] == two_back.date().isoformat()
    assert body["months"][1]["started"] is None
    # Only the full month counts toward "usual".
    assert body["average_spent"] == "2000.00"


def test_history_for_a_brand_new_account_is_just_this_month(client, user):
    token = _token(client, user)
    body = client.get("/api/v1/history", headers=_auth(token)).get_json()
    assert len(body["months"]) == 1
    assert body["months"][0]["spent"] == "0.00"
    assert body["average_spent"] is None


def test_today_says_when_apple_pay_last_logged_something(client, user):
    token = _token(client, user)
    assert client.get("/api/v1/today", headers=_auth(token)).get_json()["apple_pay"] is None
    client.post("/api/v1/transactions", headers=_auth(token), json={
        "description": "Shell", "amount": "41.10", "type": "expense", "source": "apple_pay"})
    status = client.get("/api/v1/today", headers=_auth(token)).get_json()["apple_pay"]
    assert status["description"] == "Shell"
    assert status["count"] == 1


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def test_updating_the_profile_changes_only_what_was_sent(client, user):
    user.first_name = "Alice"
    user.preferred_currency = "USD"
    db.session.commit()
    token = _token(client, user)

    resp = client.patch("/api/v1/me", headers=_auth(token), json={"currency": "GBP", "timezone": "Europe/London"})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()["user"]
    assert body["currency"] == "GBP" and body["timezone"] == "Europe/London"
    assert body["first_name"] == "Alice"

    resp = client.patch("/api/v1/me", headers=_auth(token), json={"first_name": " Al ", "email": "AL@example.com"})
    assert resp.get_json()["user"]["first_name"] == "Al"
    assert resp.get_json()["user"]["email"] == "al@example.com"


@pytest.mark.parametrize("change, message", [
    ({"email": ""}, "Email is required."),
    ({"email": "nope"}, "That doesn't look like a valid email address."),
    ({"email": "bob@example.com"}, "That email is already in use by another account."),
    ({"currency": "XTS"}, "Artha doesn't support that currency yet."),
    ({"timezone": "Mars/Olympus"}, "That isn't a time zone Artha knows."),
])
def test_profile_changes_follow_the_web_rules(client, user, change, message):
    make_user(username="bob", email="bob@example.com")
    token = _token(client, user)
    resp = client.patch("/api/v1/me", headers=_auth(token), json=change)
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "invalid_profile", "message": message}


def test_changing_the_password_signs_other_devices_out_but_keeps_this_one(client, user):
    phone = _token(client, user)
    tablet = _token(client, user)

    wrong = client.post("/api/v1/me/password", headers=_auth(phone),
                        json={"current_password": "nope", "new_password": "a-new-password"})
    assert wrong.status_code == 400
    assert wrong.get_json()["message"] == "Current password is incorrect."

    resp = client.post("/api/v1/me/password", headers=_auth(phone),
                       json={"current_password": "password123", "new_password": "a-new-password"})
    assert resp.status_code == 200
    fresh = resp.get_json()["token"]
    assert client.get("/api/v1/me", headers=_auth(fresh)).status_code == 200
    for old in (phone, tablet):
        assert client.get("/api/v1/me", headers=_auth(old)).status_code == 401
    assert db.session.get(User, user.id).check_password("a-new-password")


def test_two_factor_can_be_turned_on_and_off_from_the_app(client, user):
    token = _token(client, user)
    setup = client.post("/api/v1/me/two-factor/setup", headers=_auth(token)).get_json()
    assert setup["uri"].startswith("otpauth://totp/")
    assert "issuer=Artha" in setup["uri"]

    bad = client.post("/api/v1/me/two-factor", headers=_auth(token), json={"secret": setup["secret"], "code": "000000"})
    assert bad.status_code == 400
    assert db.session.get(User, user.id).otp_enabled is False

    code = pyotp.TOTP(setup["secret"]).now()
    on = client.post("/api/v1/me/two-factor", headers=_auth(token), json={"secret": setup["secret"], "code": code})
    assert on.status_code == 200
    assert len(on.get_json()["recovery_codes"]) == 10
    assert on.get_json()["user"]["two_factor_enabled"] is True
    assert client.post("/api/v1/me/two-factor/setup", headers=_auth(token)).status_code == 409

    # A recovery code works to turn it off, alongside the password.
    off = client.delete("/api/v1/me/two-factor", headers=_auth(token),
                        json={"password": "password123", "code": on.get_json()["recovery_codes"][0]})
    assert off.status_code == 200
    assert off.get_json()["user"]["two_factor_enabled"] is False
    assert db.session.get(User, user.id).otp_secret is None


def test_notification_preferences_are_shared_with_the_website(client, user):
    token = _token(client, user)
    me = client.get("/api/v1/me", headers=_auth(token)).get_json()["user"]
    assert me["notify_bills_due"] is True and me["notify_budget_alerts"] is True
    assert me["reminder_hour"] == 8  # the same default the reminder job uses

    resp = client.patch("/api/v1/me", headers=_auth(token),
                        json={"notify_bills_due": False, "reminder_hour": 19})
    assert resp.status_code == 200
    fresh = db.session.get(User, user.id)
    assert fresh.notify_bills_due is False and fresh.reminder_hour == 19
    assert fresh.notify_budget_alerts is True

    bad = client.patch("/api/v1/me", headers=_auth(token), json={"reminder_hour": 25, "notify_budget_alerts": False})
    assert bad.status_code == 400
    # Nothing in a rejected change is saved.
    assert db.session.get(User, user.id).notify_budget_alerts is True


def test_coming_up_lists_enough_bills_to_remind_about(client, user):
    for i in range(8):
        _add(user, f"Subscription {i}", "5.00", category="subscriptions", is_recurring=True,
             when=current_period_timestamp() - timedelta(days=20))
    token = _token(client, user)
    coming = client.get("/api/v1/today", headers=_auth(token)).get_json()["coming_up"]
    assert len(coming) == 8
