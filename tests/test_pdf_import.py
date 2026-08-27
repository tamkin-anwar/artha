import io
from types import SimpleNamespace
from unittest.mock import patch

from reportlab.lib.pagesizes import letter
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.pdfgen import canvas

from artha.extensions import db
from artha.models import Transaction


def _fake_tool_response(transactions, input_tokens=200, output_tokens=80):
    """Mirrors the Anthropic SDK response shape for a forced tool_choice
    call — see AIService.extract_pdf_transactions, which reads exactly
    this shape back out via resp.content[i].type == "tool_use"."""
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name="record_transactions",
                input={"transactions": transactions},
            )
        ],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


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


def test_preview_parses_sterling_amounts(auth_client):
    """UK statements print £ directly against the number with no space
    ("£45.23", not "£ 45.23") — the money pattern has to accept that
    exact shape or the whole line silently fails to match."""
    pdf = _make_statement_pdf([
        "05/06/2026 TESCO STORES £45.23",
    ])
    resp = _upload_pdf(auth_client, pdf)
    assert resp.status_code == 200
    rows = resp.get_json()["rows"]
    assert len(rows) == 1
    assert rows[0]["amount"] == 45.23
    assert "TESCO" in rows[0]["description"]


def test_preview_parses_euro_amounts_with_balance_column(auth_client):
    pdf = _make_statement_pdf([
        "12/03/2026 CARREFOUR PARIS €67.10 €1,200.00",
    ])
    resp = _upload_pdf(auth_client, pdf)
    assert resp.status_code == 200
    rows = resp.get_json()["rows"]
    assert len(rows) == 1
    assert rows[0]["amount"] == 67.10  # not the 1,200.00 balance


def test_preview_resolves_ambiguous_pdf_dates_day_first_with_a_day_first_currency(auth_client):
    """'05/06/2026' is ambiguous (5 June vs. May 6th) with nothing in the
    text to settle it on its own — the € elsewhere in the document is
    what tips it day-first instead of the US-first default."""
    pdf = _make_statement_pdf([
        "05/06/2026 CARREFOUR PARIS €67.10",
    ])
    resp = _upload_pdf(auth_client, pdf)
    assert resp.status_code == 200
    rows = resp.get_json()["rows"]
    assert rows[0]["date"] == "2026-06-05"


def test_preview_keeps_us_first_pdf_dates_without_a_day_first_currency(auth_client):
    pdf = _make_statement_pdf([
        "05/06/2026 COFFEE SHOP -4.50",
    ])
    resp = _upload_pdf(auth_client, pdf)
    assert resp.status_code == 200
    rows = resp.get_json()["rows"]
    assert rows[0]["date"] == "2026-05-06"


# No PDF-side ৳ (Taka) test: reportlab's default test-fixture font has no
# Bengali glyph coverage and silently mangles the character when drawn,
# which is a limitation of the test harness, not the parser — the
# symbol-stripping logic it would exercise is identical code to
# test_preview_parses_taka_amounts in test_csv_import.py, which does cover it.


def test_preview_parses_space_separated_day_month_year_dates(auth_client):
    """Common on UK statements: '19 Sep 2024' or the full month name
    '19 September 2024', rather than BRAC's hyphenated '19-Sep-2024'."""
    pdf = _make_statement_pdf([
        "Deposits and other additions",
        "19 Sep 2024 CARD PAYMENT SAINSBURYS -32.10",
        "22 September 2024 SALARY PAYMENT 1500.00",
    ])
    resp = _upload_pdf(auth_client, pdf)
    assert resp.status_code == 200
    rows = resp.get_json()["rows"]
    assert len(rows) == 2

    card = next(r for r in rows if "SAINSBURYS" in r["description"])
    assert card["date"] == "2024-09-19"
    assert card["type"] == "expense"

    salary = next(r for r in rows if "SALARY" in r["description"])
    assert salary["date"] == "2024-09-22"
    assert salary["type"] == "income"


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


# ---------------------------------------------------------------------------
# AI-assisted fallback — for a layout the line-regex parser structurally
# cannot match: a row's date+description on one line and its amounts on a
# separate line further down with no date on it at all (Bank Asia, a
# Bangladeshi bank, among real statements that print exactly this).
# ---------------------------------------------------------------------------

def test_preview_falls_back_to_ai_when_date_and_amount_are_on_separate_lines(auth_client):
    """No single-line "date ... amount" regex can ever match this shape —
    the fallback isn't a better regex, it's reading the page the way a
    person would. Row numbering matches Bank Asia's own statement layout
    (a bare row index precedes the amounts, not a date)."""
    pdf = _make_statement_pdf([
        "Debit Credit Balanced",
        "27/07/2026 PT P29 PT 202607273 VISA-POS Pos(Others)",
        "purchase with card no : 463767******8450",
        "2 2,640.00 0.00 60,282.62",
        "29/07/2026 NI DEP NPSB-Ibft Remittance from",
        "TRUST BANK LTD. A/C:0017-5*****0411",
        "5 0.00 12,064.37 70,316.99",
    ])

    with patch("artha.services.ai_service._get_client") as mock_get_client:
        mock_get_client.return_value.messages.create.return_value = _fake_tool_response([
            {
                "date": "2026-07-27",
                "description": "VISA-POS purchase with card no: 463767******8450",
                "amount": 2640.00,
                "type": "expense",
            },
            {
                "date": "2026-07-29",
                "description": "NPSB-Ibft Remittance from TRUST BANK LTD.",
                "amount": 12064.37,
                "type": "income",
            },
        ])
        resp = _upload_pdf(auth_client, pdf)

    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["rows"]) == 2
    assert data["notice"]  # distinct AI-assisted heads-up, not folded into warnings

    expense = next(r for r in data["rows"] if r["type"] == "expense")
    assert expense["date"] == "2026-07-27"
    assert expense["amount"] == 2640.00

    income = next(r for r in data["rows"] if r["type"] == "income")
    assert income["date"] == "2026-07-29"
    assert income["amount"] == 12064.37

    # The forced-tool call actually happened with this document's text —
    # not a coincidental empty-input no-op.
    call_kwargs = mock_get_client.return_value.messages.create.call_args.kwargs
    assert call_kwargs["tool_choice"] == {"type": "tool", "name": "record_transactions"}
    assert "VISA-POS" in call_kwargs["messages"][0]["content"]


def test_preview_does_not_call_ai_when_regex_parsing_already_succeeds(auth_client):
    """The fallback only fires on zero rows — a statement the regex
    already handles shouldn't pay for an API call it doesn't need."""
    pdf = _make_statement_pdf([
        "05/06/2026 COFFEE SHOP -4.50",
    ])
    with patch("artha.services.ai_service._get_client") as mock_get_client:
        resp = _upload_pdf(auth_client, pdf)
        mock_get_client.assert_not_called()

    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["rows"]) == 1
    assert "notice" not in data


def test_preview_ai_fallback_degrades_silently_without_api_key(auth_client, monkeypatch):
    """No ANTHROPIC_API_KEY configured (the normal state for a dev/test
    environment) shouldn't surface an AI-specific error — from the user's
    side this is just an unsupported PDF layout, same message either way."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # _get_client() caches its client in a module-level singleton once
    # initialized — force a fresh (and here, failing) init regardless of
    # whatever state an earlier test in the same process left behind.
    monkeypatch.setattr("artha.services.ai_service._client", None)
    pdf = _make_statement_pdf([
        "27/07/2026 PT P29 PT 202607273 VISA-POS Pos(Others)",
        "purchase with card no : 463767******8450",
        "2 2,640.00 0.00 60,282.62",
    ])
    resp = _upload_pdf(auth_client, pdf)
    assert resp.status_code == 400
    assert "nothing that looked like a transaction line" in resp.get_json()["message"]


# ---------------------------------------------------------------------------
# Row-numbered ledger layout (Bank Asia, a Bangladeshi bank, prints exactly
# this: every line prefixed with its own row index before the date) — the
# real reported bug. Parsed entirely deterministically, no AI involved.
# ---------------------------------------------------------------------------

def test_preview_parses_row_numbered_statement_with_correct_day_first_dates(auth_client):
    """Two things have to both work here: the leading row-number prefix
    ("2 27/07/2026 ...") not breaking the date match, and an ambiguous
    date ("03/08/2026") resolving day-first purely from the unambiguous
    dates elsewhere in the same statement — this bank spells its currency
    out as "BDT" rather than printing the Taka symbol, so the older
    symbol-only day-first detection would have silently misread it as
    March 8 instead of August 3."""
    pdf = _make_statement_pdf([
        "Currency BDT - Bangladeshi Taka",
        "Debit Credit Balanced",
        "1 26/07/2026 Balance 0.00 0.00 62,922.62",
        "2 27/07/2026 VISA-POS purchase 2,640.00 0.00 60,282.62",
        "3 03/08/2026 NPSB-MPOS purchase 750.35 0.00 59,532.27",
        "4 25/08/2026 Total Transaction Amount 3,390.35 0.00",
    ])
    resp = _upload_pdf(auth_client, pdf)
    assert resp.status_code == 200
    rows = resp.get_json()["rows"]

    # Row 1 (0.00/0.00 opening balance) and row 4 (closing summary) are
    # both correctly excluded — only the two real transactions remain.
    assert len(rows) == 2

    visa = next(r for r in rows if "VISA" in r["description"])
    assert visa["date"] == "2026-07-27"
    assert visa["type"] == "expense"
    assert visa["amount"] == 2640.00

    npsb = next(r for r in rows if "NPSB" in r["description"])
    assert npsb["date"] == "2026-08-03"  # not 2026-03-08
    assert npsb["type"] == "expense"
    assert npsb["amount"] == 750.35


def test_preview_recovers_a_page_break_corrupted_row_via_ai_without_duplicating(auth_client):
    """A transaction landing right at a page boundary can have its own
    date fragmented across the split (a real artifact seen in Bank Asia's
    export: "05/08/2" ends one page, the "026" completing the year starts
    the next). That single line fails to match any regex while everything
    else on the statement parses fine — the AI pass should fill in just
    that one gap, not re-import everything else a second time."""
    pdf = _make_statement_pdf([
        "1 26/07/2026 Balance 0.00 0.00 62,922.62",
        "2 27/07/2026 VISA-POS purchase 2,640.00 0.00 60,282.62",
        "3 05/08/2 WB WWL WB From Ridita 2,500.00 0.00 57,782.62",
    ])

    with patch("artha.services.ai_service._get_client") as mock_get_client:
        mock_get_client.return_value.messages.create.return_value = _fake_tool_response([
            {
                "date": "2026-08-05",
                "description": "WB WWL WB From Ridita",
                "amount": 2500.00,
                "type": "expense",
            },
            # A real AI pass over the *whole* document also re-reads the
            # already-correctly-parsed VISA-POS row — the merge logic
            # needs to drop this exact duplicate, not double-import it.
            {
                "date": "2026-07-27",
                "description": "VISA-POS purchase",
                "amount": 2640.00,
                "type": "expense",
            },
        ])
        resp = _upload_pdf(auth_client, pdf)

    assert resp.status_code == 200
    data = resp.get_json()
    rows = data["rows"]
    assert len(rows) == 2  # not 3 — the VISA-POS duplicate was dropped
    assert data["notice"]  # AI contributed at least one row, worth flagging

    ridita = next(r for r in rows if "Ridita" in r["description"])
    assert ridita["date"] == "2026-08-05"
    assert ridita["amount"] == 2500.00

    visa_rows = [r for r in rows if "VISA" in r["description"]]
    assert len(visa_rows) == 1
