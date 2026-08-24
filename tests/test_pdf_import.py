import io

from reportlab.lib.pagesizes import letter
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.pdfgen import canvas

from artha.extensions import db
from artha.models import Transaction


def _make_statement_pdf(lines, password=None):
    """
    Builds a minimal text-based PDF by drawing plain lines of text —
    mirrors how a real bank e-statement extracts (individual text lines,
    not an actual drawn table with gridlines), which is what the PDF
    parser is built against. `lines` is a flat list of strings, one per
    line, across as many pages as needed. `password`, if given, encrypts
    the PDF the same way a bank's password-protected e-statement export
    does (BRAC Bank among others makes this the default for Bangladesh).
    """
    buffer = io.BytesIO()
    encrypt = StandardEncryption(password, canPrint=1, canModify=0, canCopy=1, canAnnotate=0) if password else None
    c = canvas.Canvas(buffer, pagesize=letter, encrypt=encrypt)
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


def _upload_pdf(client, buffer, filename="statement.pdf", password=None):
    data = {"statement": (buffer, filename)}
    if password is not None:
        data["pdf_password"] = password
    return client.post(
        "/finance/import/preview",
        data=data,
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


def test_preview_asks_for_password_on_encrypted_pdf(auth_client):
    pdf = _make_statement_pdf(["06/17/26 NETFLIX.COM -15.49"], password="secret123")
    resp = _upload_pdf(auth_client, pdf)
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["needs_password"] is True


def test_preview_reports_wrong_password_distinctly(auth_client):
    pdf = _make_statement_pdf(["06/17/26 NETFLIX.COM -15.49"], password="secret123")
    resp = _upload_pdf(auth_client, pdf, password="wrong-guess")
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["needs_password"] is True
    assert "didn't work" in body["message"]


def test_preview_parses_encrypted_pdf_with_correct_password(auth_client):
    pdf = _make_statement_pdf(["06/17/26 NETFLIX.COM -15.49"], password="secret123")
    resp = _upload_pdf(auth_client, pdf, password="secret123")
    assert resp.status_code == 200
    rows = resp.get_json()["rows"]
    assert len(rows) == 1
    assert rows[0]["type"] == "expense"
    assert rows[0]["amount"] == 15.49


def test_preview_parses_day_month_year_dates_with_separate_withdraw_deposit_columns(auth_client):
    """Matches BRAC Bank's format (common across Bangladeshi banks): dates
    as '19-Sep-2024' rather than MM/DD, and separate withdraw/deposit/
    balance columns rather than one signed amount."""
    pdf = _make_statement_pdf([
        "DATE PARTICULARS CHQ.NO WITHDRAW DEPOSIT BALANCE",
        "19-Sep-2024 CARD ANNUAL FEE **8562 FOR 2024-25 600.00 0.00 4,400.00",
        "05-Jan-2025 BKASH/FUND RCVD/01786156679 0.00 100.00 3,800.00",
    ])
    resp = _upload_pdf(auth_client, pdf)
    assert resp.status_code == 200
    rows = resp.get_json()["rows"]
    assert len(rows) == 2

    fee = next(r for r in rows if "CARD ANNUAL FEE" in r["description"])
    assert fee["date"] == "2024-09-19"
    assert fee["type"] == "expense"
    assert fee["amount"] == 600.00

    deposit = next(r for r in rows if "BKASH" in r["description"])
    assert deposit["date"] == "2025-01-05"
    assert deposit["type"] == "income"
    assert deposit["amount"] == 100.00
