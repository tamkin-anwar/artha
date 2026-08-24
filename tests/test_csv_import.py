import io
from datetime import date, datetime, timezone
from decimal import Decimal

from artha.extensions import db
from artha.models import Transaction


def _upload(client, csv_text, filename="statement.csv"):
    return client.post(
        "/finance/import/preview",
        data={"statement": (io.BytesIO(csv_text.encode("utf-8")), filename)},
        content_type="multipart/form-data",
    )


def test_preview_parses_standard_columns(auth_client):
    csv_text = (
        "Date,Description,Amount\n"
        "2026-03-05,Whole Foods,-42.10\n"
        "2026-03-06,Paycheck,2000.00\n"
    )
    resp = _upload(auth_client, csv_text)
    assert resp.status_code == 200
    data = resp.get_json()
    rows = data["rows"]
    assert len(rows) == 2

    groceries = next(r for r in rows if r["description"] == "Whole Foods")
    assert groceries["date"] == "2026-03-05"
    assert groceries["amount"] == 42.10
    assert groceries["type"] == "expense"

    paycheck = next(r for r in rows if r["description"] == "Paycheck")
    assert paycheck["type"] == "income"
    assert paycheck["category"] == "income"


def test_preview_supports_debit_credit_columns(auth_client):
    csv_text = (
        "Transaction Date,Description,Debit,Credit\n"
        "01/15/2026,Shell Gas Station,35.00,\n"
        "01/16/2026,Direct Deposit,,1500.00\n"
    )
    resp = _upload(auth_client, csv_text)
    assert resp.status_code == 200
    rows = resp.get_json()["rows"]

    gas = next(r for r in rows if "Shell" in r["description"])
    assert gas["type"] == "expense"
    assert gas["amount"] == 35.00
    assert gas["date"] == "2026-01-15"

    deposit = next(r for r in rows if "Deposit" in r["description"])
    assert deposit["type"] == "income"
    assert deposit["amount"] == 1500.00


def test_preview_auto_suggests_category_from_merchant_name(auth_client):
    csv_text = "Date,Description,Amount\n2026-04-01,NETFLIX.COM,-15.49\n"
    resp = _upload(auth_client, csv_text)
    rows = resp.get_json()["rows"]
    assert rows[0]["category"] == "subscriptions"


def test_preview_rejects_file_without_recognizable_columns(auth_client):
    csv_text = "Foo,Bar\n1,2\n"
    resp = _upload(auth_client, csv_text)
    assert resp.status_code == 400
    assert "column" in resp.get_json()["message"].lower()


def test_preview_flags_rows_matching_existing_transactions_as_duplicates(auth_client, user):
    existing = Transaction(
        description="Whole Foods",
        amount=Decimal("42.10"),
        type="expense",
        user_id=user.id,
        timestamp=datetime(2026, 3, 5, 12, 0, 0, tzinfo=timezone.utc),
    )
    db.session.add(existing)
    db.session.commit()

    csv_text = "Date,Description,Amount\n2026-03-05,Whole Foods,-42.10\n"
    resp = _upload(auth_client, csv_text)
    rows = resp.get_json()["rows"]
    assert rows[0]["duplicate"] is True


def test_commit_preserves_statement_date_regardless_of_when_uploaded(auth_client, user):
    """The whole point of import: a March statement uploaded today (whatever
    today actually is) must land in March, never on the upload date."""
    resp = auth_client.post(
        "/finance/import/commit",
        json={"rows": [
            {"date": "2026-03-15", "description": "Rent", "amount": 1200.00, "type": "expense", "category": "housing"},
        ]},
    )
    assert resp.status_code == 200
    assert resp.get_json()["imported"] == 1

    tx = Transaction.query.filter_by(user_id=user.id).first()
    assert tx is not None
    assert tx.timestamp.strftime("%Y-%m-%d") == "2026-03-15"
    # Never silently defaulted to today, whatever today happens to be.
    assert tx.timestamp.date() != date.today() or date.today() == date(2026, 3, 15)
    assert tx.import_source == "csv"
    assert tx.category == "housing"


def test_commit_skips_rows_with_unparseable_date(auth_client, user):
    resp = auth_client.post(
        "/finance/import/commit",
        json={"rows": [
            {"date": "not-a-date", "description": "Bad row", "amount": 10.00, "type": "expense"},
            {"date": "2026-05-01", "description": "Good row", "amount": 20.00, "type": "expense"},
        ]},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["imported"] == 1
    assert data["skipped"] == 1
    assert Transaction.query.filter_by(user_id=user.id).count() == 1


def test_commit_ignores_unknown_category(auth_client, user):
    auth_client.post(
        "/finance/import/commit",
        json={"rows": [
            {"date": "2026-02-01", "description": "Unknown merchant", "amount": 5.00, "type": "expense", "category": "nonsense"},
        ]},
    )
    tx = Transaction.query.filter_by(user_id=user.id).first()
    assert tx.category is None


def test_commit_requires_login(client):
    resp = client.post("/finance/import/commit", json={"rows": []})
    assert resp.status_code in (302, 401)
