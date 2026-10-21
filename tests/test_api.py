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
from artha.models.budget import Budget
from artha.services.month_snapshot import build_month_snapshot

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
    assert first["next_offset"] == 2

    second = client.get("/api/v1/transactions?limit=2&offset=2", headers=_auth(token)).get_json()
    assert [t["description"] for t in second["transactions"]] == ["Item 2"]
    assert second["next_offset"] is None

    assert client.get("/api/v1/transactions?limit=abc", headers=_auth(token)).status_code == 400


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
