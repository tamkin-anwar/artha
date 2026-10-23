"""One purchase across categories (services/splits.py): each part is its
own row so every total counts it under its own category, while Activity
shows the purchase once at its full amount."""

from datetime import date, datetime
from decimal import Decimal

from artha.extensions import db
from artha.models import Transaction

from .conftest import current_period_timestamp


def _purchase(user, amount="100.00", description="COSTCO WHSE #0432", category="groceries", currency="USD", usd=None, **extra):
    tx = Transaction(
        user_id=user.id, description=description, amount=Decimal(amount), type="expense",
        category=category, currency=currency, usd_value=Decimal(usd or amount),
        timestamp=current_period_timestamp(), import_source="csv", **extra,
    )
    db.session.add(tx)
    db.session.commit()
    return tx


def _split(client, tx_id, *parts):
    return client.post(f"/activity/{tx_id}/split", json={"parts": [
        {"amount": amount, "category": category} for amount, category in parts
    ]})


def _api_headers(client, user):
    token = client.post("/api/v1/sessions", json={"username": user.username, "password": "password123"}).get_json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_each_part_counts_under_its_own_category(auth_client, user):
    tx = _purchase(user)
    resp = _split(auth_client, tx.id, ("60.00", "groceries"), ("40.00", "shopping"))
    assert resp.status_code == 200, resp.get_json()

    rows = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.id).all()
    assert [(r.amount, r.category) for r in rows] == [(Decimal("60.00"), "groceries"), (Decimal("40.00"), "shopping")]
    assert rows[0].split_group and rows[0].split_group == rows[1].split_group
    assert all(r.description == "COSTCO WHSE #0432" and r.timestamp == tx.timestamp for r in rows)
    assert sum(r.usd_value for r in rows) == Decimal("100.00")


def test_the_parts_must_add_up_to_the_purchase(auth_client, user):
    tx = _purchase(user)
    resp = _split(auth_client, tx.id, ("60.00", "groceries"), ("30.00", "shopping"))
    assert resp.status_code == 400
    assert "10.00 is left to assign" in resp.get_json()["message"]
    assert _split(auth_client, tx.id, ("60.00", "groceries")).status_code == 400  # one part isn't a split
    assert _split(auth_client, tx.id, ("60.001", "groceries"), ("39.999", None)).status_code == 400
    assert Transaction.query.filter_by(user_id=user.id).count() == 1


def test_activity_shows_one_purchase_and_a_category_shows_its_part(client, user):
    tx = _purchase(user)
    _purchase(user, "5.00", "STARBUCKS", "dining")
    headers = _api_headers(client, user)
    client.post(f"/api/v1/transactions/{tx.id}/split", headers=headers,
                json={"parts": [{"amount": "60.00", "category": "groceries"}, {"amount": "40.00", "category": "shopping"}]})

    listed = client.get("/api/v1/transactions", headers=headers).get_json()["transactions"]
    costco = [t for t in listed if t["description"].startswith("COSTCO")]
    assert len(costco) == 1
    assert costco[0]["amount"] == "100.00" and costco[0]["category_label"] == "Split: Groceries, Shopping"
    assert [(p["amount"], p["category"]) for p in costco[0]["split"]] == [("60.00", "groceries"), ("40.00", "shopping")]
    assert costco[0]["can_pair"] is False

    shopping = client.get("/api/v1/transactions?category=shopping", headers=headers).get_json()["transactions"]
    assert [(t["amount"], t["category"]) for t in shopping] == [("40.00", "shopping")]
    assert shopping[0]["split"]


def test_renaming_or_redating_a_split_purchase_changes_every_part(auth_client, user):
    tx = _purchase(user)
    _split(auth_client, tx.id, ("60.00", "groceries"), ("40.00", "shopping"))
    resp = auth_client.post(f"/update_transaction/{tx.id}", json={
        "description": "Costco", "amount": "100.00", "category": "dining", "date": "2026-10-01",
    })
    assert resp.status_code == 200, resp.get_json()
    rows = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.id).all()
    assert {r.description for r in rows} == {"Costco"}
    assert {r.timestamp.date() for r in rows} == {date(2026, 10, 1)}
    assert [r.category for r in rows] == ["groceries", "shopping"]  # categories live in the split

    refused = auth_client.post(f"/update_transaction/{tx.id}", json={"amount": "120.00"})
    assert refused.status_code == 400
    assert "Split" in refused.get_json()["message"]


def test_resplitting_changes_the_parts_and_unsplit_joins_them(auth_client, user):
    tx = _purchase(user)
    _split(auth_client, tx.id, ("60.00", "groceries"), ("40.00", "shopping"))
    _split(auth_client, tx.id, ("50.00", "groceries"), ("30.00", "shopping"), ("20.00", "health"))
    rows = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.id).all()
    assert [r.amount for r in rows] == [Decimal("50.00"), Decimal("30.00"), Decimal("20.00")]

    _split(auth_client, tx.id, ("70.00", "groceries"), ("30.00", "shopping"))
    assert Transaction.query.filter_by(user_id=user.id).count() == 2

    resp = auth_client.post(f"/activity/{tx.id}/unsplit")
    assert resp.status_code == 200
    joined = Transaction.query.filter_by(user_id=user.id).one()
    assert (joined.amount, joined.category, joined.split_group) == (Decimal("100.00"), "groceries", None)
    assert joined.usd_value == Decimal("100.00")


def test_deleting_a_split_purchase_deletes_all_of_it(client, user):
    tx = _purchase(user)
    headers = _api_headers(client, user)
    client.post(f"/api/v1/transactions/{tx.id}/split", headers=headers,
                json={"parts": [{"amount": "60.00", "category": "groceries"}, {"amount": "40.00", "category": "shopping"}]})
    assert client.delete(f"/api/v1/transactions/{tx.id}", headers=headers).status_code in (200, 204)
    assert Transaction.query.filter_by(user_id=user.id).count() == 0


def test_a_foreign_purchase_keeps_its_locked_dollar_value(auth_client, user):
    tx = _purchase(user, amount="1000.00", currency="BDT", usd="8.33")
    _split(auth_client, tx.id, ("333.00", "groceries"), ("333.00", "shopping"), ("334.00", None))
    rows = Transaction.query.filter_by(user_id=user.id).all()
    assert sum(r.usd_value for r in rows) == Decimal("8.33")
    assert all(r.currency == "BDT" for r in rows)


def test_a_bill_cant_be_split(auth_client, user):
    tx = _purchase(user, "15.49", "Netflix", "subscriptions", is_recurring=True)
    resp = _split(auth_client, tx.id, ("10.00", "subscriptions"), ("5.49", "entertainment"))
    assert resp.status_code == 400
    assert Transaction.query.filter_by(user_id=user.id).count() == 1


def test_reimporting_a_statement_knows_the_split_purchase(auth_client, user):
    import io
    tx = _purchase(user, description="COSTCO WHSE")
    _split(auth_client, tx.id, ("60.00", "groceries"), ("40.00", "shopping"))
    day = tx.timestamp.date().isoformat()
    csv = f"Date,Description,Amount\n{day},COSTCO WHSE,-100.00\n".encode()
    resp = auth_client.post("/finance/import/preview", data={"statement": (io.BytesIO(csv), "s.csv")},
                            content_type="multipart/form-data")
    assert resp.get_json()["rows"][0]["duplicate"] is True
