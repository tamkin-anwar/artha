import io

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from artha.extensions import db
from artha.models import Transaction


def _make_statement_pdf(lines):
    """
    Builds a minimal text-based PDF by drawing plain lines of text —
    mirrors how a real bank e-statement extracts (individual text lines,
    not an actual drawn table with gridlines), which is what the PDF
    parser is built against. `lines` is a flat list of strings, one per
    line, across as many pages as needed.
    """
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    y = 750
    for line in lines:
        if y < 50:
            c.showPage()
            y = 750
        c.drawString(50, y, line)
        y -= 14
    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


def _upload_pdf(client, buffer, filename="statement.pdf"):
    return client.post(
        "/finance/import/preview",
        data={"statement": (buffer, filename)},
        content_type="multipart/form-data",
    )


def test_preview_parses_signed_amounts_directly(auth_client):
    pdf = _make_statement_pdf([
        "Deposits and other additions",
        "Date Description Amount",
        "06/05/26 AMAZON.COM SERVICES:PAYROLL 953.59",
        "Withdrawals and other subtractions",
        "Date Description Amount",
        "06/05/26 PADDLE.NET* SETAPP -10.86",
    ])
    resp = _upload_pdf(auth_client, pdf)
    assert resp.status_code == 200
    rows = resp.get_json()["rows"]
    assert len(rows) == 2

    payroll = next(r for r in rows if "AMAZON" in r["description"])
    assert payroll["date"] == "2026-06-05"
    assert payroll["type"] == "income"
    assert payroll["amount"] == 953.59

    setapp = next(r for r in rows if "SETAPP" in r["description"])
    assert setapp["type"] == "expense"
    assert setapp["amount"] == 10.86


def test_preview_defaults_unsigned_amount_to_income(auth_client):
    """No section-header fallback: real statements broke it (a summary
    block's "Withdrawals" line went stale before real deposit rows, which
    were unsigned, ever appeared) — the amount's own sign is the only
    signal now. Unsigned means income; a wrongly-typed unsigned expense
    row is exactly what the preview's editable Type column is for."""
    pdf = _make_statement_pdf([
        "Date Description Amount",
        "07/02/26 Direct Deposit Payroll 2500.00",
    ])
    resp = _upload_pdf(auth_client, pdf)
    rows = resp.get_json()["rows"]
    assert rows[0]["type"] == "income"
    assert rows[0]["amount"] == 2500.00


def test_preview_handles_running_balance_column_and_bare_month_day_dates(auth_client):
    """Matches Chase's format: 'MM/DD Description amount balance' with no
    year on the line at all — the year comes from the statement-period
    text instead."""
    pdf = _make_statement_pdf([
        "June 24, 2026 through July 22, 2026",
        "DATE DESCRIPTION AMOUNT BALANCE",
        "07/02 Nu -Osv Payroll492 PPD ID: 00016514 531.05 531.52",
        "07/06 Card Purchase 07/04 Shell Oil12867495025 Corona CA Card 7505 -3.52 941.82",
    ])
    resp = _upload_pdf(auth_client, pdf)
    rows = resp.get_json()["rows"]
    assert len(rows) == 2

    payroll = next(r for r in rows if "Payroll" in r["description"])
    assert payroll["date"] == "2026-07-02"
    assert payroll["type"] == "income"
    assert payroll["amount"] == 531.05  # not 531.52 — that's the balance, not the amount

    shell = next(r for r in rows if "Shell" in r["description"])
    assert shell["date"] == "2026-07-06"
    assert shell["type"] == "expense"
    assert shell["amount"] == 3.52


def test_preview_ignores_continuation_and_total_lines(auth_client):
    pdf = _make_statement_pdf([
        "Deposits and other additions",
        "Date Description Amount",
        "06/12/26 FID BKG SVC LLC DES:MONEYLINE ID:Z38445308 90.00",
        "ID:1035141375 WEB",
        "Total deposits and other additions $90.00",
    ])
    resp = _upload_pdf(auth_client, pdf)
    rows = resp.get_json()["rows"]
    assert len(rows) == 1
    assert rows[0]["amount"] == 90.00


def test_preview_auto_suggests_category_from_pdf_merchant_name(auth_client):
    pdf = _make_statement_pdf([
        "Withdrawals and other subtractions",
        "06/17/26 AMC THEATRES 0616 CORONA CA -14.50",
    ])
    rows = _upload_pdf(auth_client, pdf).get_json()["rows"]
    assert rows[0]["category"] == "entertainment"


def test_preview_dispatches_to_pdf_parser_by_uppercase_extension(auth_client):
    pdf = _make_statement_pdf(["06/17/26 NETFLIX.COM -15.49"])
    resp = auth_client.post(
        "/finance/import/preview",
        data={"statement": (pdf, "statement.PDF")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    rows = resp.get_json()["rows"]
    assert rows[0]["category"] == "subscriptions"


def test_preview_rejects_pdf_with_no_transaction_lines(auth_client):
    pdf = _make_statement_pdf(["Just some prose, no transactions here."])
    resp = _upload_pdf(auth_client, pdf)
    assert resp.status_code == 400
    assert "transaction" in resp.get_json()["message"].lower()


def test_preview_rejects_corrupted_pdf(auth_client):
    resp = _upload_pdf(auth_client, io.BytesIO(b"not a real pdf file"))
    assert resp.status_code == 400


def test_commit_after_pdf_preview_preserves_statement_date(auth_client, user):
    pdf = _make_statement_pdf([
        "Withdrawals and other subtractions",
        "03/15/26 Rent -1200.00",
    ])
    preview = _upload_pdf(auth_client, pdf)
    rows = preview.get_json()["rows"]
    assert rows[0]["date"] == "2026-03-15"
    assert rows[0]["type"] == "expense"

    resp = auth_client.post("/finance/import/commit", json={"rows": rows})
    assert resp.status_code == 200
    assert resp.get_json()["imported"] == 1

    tx = Transaction.query.filter_by(user_id=user.id).first()
    assert tx.timestamp.strftime("%Y-%m-%d") == "2026-03-15"
    assert tx.import_source == "csv"
