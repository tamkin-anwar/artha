"""Importing a statement from the app (POST /api/v1/imports/preview and
/api/v1/imports): the same reading and saving as the website's import
sheet (finance.routes.preview_statement / import_statement_rows), with
money as strings like the rest of the API."""

import io
from datetime import datetime, timezone
from decimal import Decimal

from artha.extensions import db
from artha.models import Transaction

from .test_pdf_import import _make_statement_pdf

CSV = (
    "Date,Description,Amount\n"
    "2026-03-05,Whole Foods,-42.10\n"
    "2026-03-06,Paycheck,2000.00\n"
)


def _headers(client, user):
    token = client.post("/api/v1/sessions", json={"username": user.username, "password": "password123"}).get_json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _preview(client, headers, body, filename="statement.csv", password=None):
    data = {"statement": (io.BytesIO(body) if isinstance(body, bytes) else body, filename)}
    if password is not None:
        data["pdf_password"] = password
    return client.post("/api/v1/imports/preview", headers=headers, data=data, content_type="multipart/form-data")


def test_the_app_previews_a_csv_and_saves_what_it_keeps(client, user):
    headers = _headers(client, user)

    resp = _preview(client, headers, CSV.encode())

    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    rows = {r["description"]: r for r in body["rows"]}
    groceries = rows["Whole Foods"]
    assert groceries["amount"] == "42.10" and groceries["type"] == "expense"
    assert groceries["date"] == "2026-03-05" and groceries["currency"] == "USD"
    assert groceries["duplicate"] is False and groceries["category_label"]
    assert rows["Paycheck"]["type"] == "income" and rows["Paycheck"]["amount"] == "2000.00"
    assert body["notice"] is None
    assert Transaction.query.filter_by(user_id=user.id).count() == 0  # nothing saved yet

    # Keep only the groceries, renamed.
    keep = dict(groceries, description="Whole Foods Market")
    saved = client.post("/api/v1/imports", headers=headers, json={"rows": [keep]})

    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json() == {"imported": 1, "skipped": 0}
    tx = Transaction.query.filter_by(user_id=user.id).one()
    assert tx.description == "Whole Foods Market" and tx.amount == Decimal("42.10")
    assert tx.timestamp.date().isoformat() == "2026-03-05" and tx.import_source == "csv"


def test_a_second_preview_marks_what_is_already_in(client, user):
    headers = _headers(client, user)
    db.session.add(Transaction(description="Whole Foods", amount=Decimal("42.10"), type="expense", user_id=user.id,
                               currency="USD", timestamp=datetime(2026, 3, 5, 12, tzinfo=timezone.utc)))
    db.session.commit()

    rows = {r["description"]: r for r in _preview(client, headers, CSV.encode()).get_json()["rows"]}

    assert rows["Whole Foods"]["duplicate"] is True and rows["Paycheck"]["duplicate"] is False


def test_a_locked_pdf_asks_for_its_password(client, user):
    headers = _headers(client, user)
    lines = ["Date Description Amount", "07/02/26 Direct Deposit Payroll 2500.00"]

    first = _preview(client, headers, _make_statement_pdf(lines, password="s3cret"), "statement.pdf")
    wrong = _preview(client, headers, _make_statement_pdf(lines, password="s3cret"), "statement.pdf", password="nope")
    right = _preview(client, headers, _make_statement_pdf(lines, password="s3cret"), "statement.pdf", password="s3cret")

    assert first.status_code == 400 and first.get_json()["error"] == "needs_password"
    assert wrong.status_code == 400 and wrong.get_json()["error"] == "wrong_password"
    assert right.status_code == 200 and right.get_json()["rows"][0]["amount"] == "2500.00"


def test_an_unreadable_file_says_so(client, user):
    headers = _headers(client, user)

    resp = _preview(client, headers, b"not,a,statement\n", "notes.csv")

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "unreadable_statement" and resp.get_json()["message"]


def test_it_needs_the_app_to_be_signed_in(client, user):
    assert _preview(client, {}, CSV.encode()).status_code == 401
    assert client.post("/api/v1/imports", json={"rows": []}).status_code == 401


def test_nothing_to_import_is_an_error_not_a_save(client, user):
    headers = _headers(client, user)
    resp = client.post("/api/v1/imports", headers=headers, json={"rows": []})
    assert resp.status_code == 400 and resp.get_json()["error"] == "invalid_import"
