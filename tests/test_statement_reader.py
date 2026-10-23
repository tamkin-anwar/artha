"""Reading statements the way banks around the world write them
(services/statement_reader.py and the PDF/CSV/OFX/QIF paths through
finance.routes.preview_statement), and finishing in time: a real Bank of
America statement used to go to AI over its "Beginning balance" line and
run past the server's 30-second limit (2026-10-23)."""

import io
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from artha.extensions import db
from artha.models import Transaction
from artha.services import statement_reader as sr

from .test_pdf_import import _fake_tool_response, _make_statement_pdf, _upload_pdf


def _upload(client, body: bytes, filename: str):
    return client.post(
        "/finance/import/preview",
        data={"statement": (io.BytesIO(body), filename)},
        content_type="multipart/form-data",
    )


def _row(rows, word):
    return next(r for r in rows if word.lower() in r["description"].lower())


# ---------------------------------------------------------------------------
# Amounts, dates, names
# ---------------------------------------------------------------------------

def test_amounts_in_every_common_notation():
    cases = {
        "1,234.56": (Decimal("1234.56"), False),
        "1.234,56": (Decimal("1234.56"), False),
        "1'234.56": (Decimal("1234.56"), False),
        "12,34,567.89": (Decimal("1234567.89"), False),
        "-$20,705.29": (Decimal("-20705.29"), True),
        "(45.00)": (Decimal("-45.00"), True),
        "45.00-": (Decimal("-45.00"), True),
        "450.00 Dr": (Decimal("-450.00"), True),
        "450.00 CR": (Decimal("450.00"), True),
        "+1.500,00": (Decimal("1500.00"), True),
        "€ 23,45": (Decimal("23.45"), False),
        "EUR 23,45": (Decimal("23.45"), False),
        "−12.00": (Decimal("-12.00"), True),
    }
    for raw, expected in cases.items():
        assert sr.parse_amount(raw) == expected, raw


def test_phone_numbers_and_references_are_not_money():
    assert sr.amounts_in("Customer service: 1.800.432.1000") == []
    assert sr.amounts_in("Confirmation# 5265694029 1,650.00") == ["1,650.00"]


def test_dates_in_every_common_notation():
    anchor = date(2026, 10, 6)
    assert sr.parse_date("09/08/26") == date(2026, 9, 8)
    assert sr.parse_date("05.09.2026") == date(2026, 9, 5)       # always day-first
    assert sr.parse_date("05.09.", anchor=anchor) == date(2026, 9, 5)
    assert sr.parse_date("2026-09-05") == date(2026, 9, 5)
    assert sr.parse_date("5 Sep", anchor=anchor) == date(2026, 9, 5)
    assert sr.parse_date("Sep 5", anchor=anchor) == date(2026, 9, 5)
    assert sr.parse_date("19-Sep-24") == date(2024, 9, 19)
    assert sr.parse_date("5 sept. 2026") == date(2026, 9, 5)
    assert sr.parse_date("05 Okt 2026") == date(2026, 10, 5)
    assert sr.parse_date("12 dic 2026") == date(2026, 12, 12)
    assert sr.parse_date("2026年9月5日") == date(2026, 9, 5)
    assert sr.parse_date("03/04/2026", day_first=True) == date(2026, 4, 3)
    # A December line on a January statement is last year's.
    assert sr.parse_date("12/28", anchor=date(2027, 1, 20)) == date(2026, 12, 28)


def test_bank_lines_become_names_a_person_would_use():
    assert sr.clean_description("MOBILE PURCHASE 0909 STARBUCKS STORE 51971 CORONA CA") == "Starbucks Store Corona CA"
    assert sr.clean_description("PENNYMAC DES:CASH ID:8204360824-0038 INDN:A PERSON CO ID:1234567 PPD") == "Pennymac"
    assert sr.clean_description("DISCOVER DES:E-PAYMENT ID:1305 INDN:A PERSON CO ID:2510020270 WEB") == "Discover E-payment"
    assert sr.clean_description("CHECKCARD 0907 GOOGLE *CHROME 888-986-7944 CA 24036296250716388800906") == "Google *Chrome CA"
    assert sr.clean_description("Online Banking transfer from SAV 7837 Confirmation# 5265694029") == "Online Banking transfer from SAV"
    assert sr.clean_description("CHECKCARD 0907 Aspire General Rancho CucamoCA") == "Aspire General Rancho Cucamo CA"
    assert sr.clean_description("SHELL OIL12867495025 CORONA CA") == "Shell Oil Corona CA"
    # Never splits a name that simply ends in a state's letters.
    assert sr.clean_description("COSTCO") == "Costco"
    assert sr.clean_description("VENMO DES:CASHOUT ID:1 INDN:A PERSON CO") == "Venmo Cashout"
    # Generic words only: the bank's wording stays.
    assert sr.clean_description("VISA-POS purchase") == "VISA-POS purchase"
    # A person's own mixed-case name is left alone.
    assert sr.clean_description("Zelle payment to Sam Lee for rent") == "Zelle payment to Sam Lee for rent"


def test_transfers_between_your_own_accounts():
    assert sr.is_own_transfer("Online Banking transfer to SAV 7837 Confirmation# 1", "", None, None)
    assert sr.is_own_transfer("VENMO DES:CASHOUT ID:1", "Venmo Cashout", None, None)
    assert sr.is_own_transfer("Transfer SAM Q LEE", "Transfer SAM Q LEE", "Sam", "Lee")
    assert not sr.is_own_transfer("Zelle payment to SAM LEE", "Zelle payment to SAM LEE", "Sam", "Lee")
    assert not sr.is_own_transfer("Zelle payment from A FRIEND", "Zelle payment from A FRIEND", "Sam", "Lee")
    assert not sr.is_own_transfer("STARBUCKS", "Starbucks", "Sam", "Lee")


def test_totals_prove_an_import_complete():
    text = "Beginning balance on September 5, 2026 $326.44\nEnding balance on October 6, 2026 $242.85"
    rows = [{"amount": 100.0, "type": "income"}, {"amount": 183.59, "type": "expense"}]
    assert sr.check_totals(text, rows) == "match"
    assert sr.check_totals(text, rows[:1]) == "mismatch"
    assert sr.check_totals("no totals here", rows) is None


# ---------------------------------------------------------------------------
# PDF layouts from around the world
# ---------------------------------------------------------------------------

def test_a_us_statement_that_reads_cleanly_never_goes_to_ai(auth_client):
    """Bank of America's layout: a summary block with balances and phone
    numbers above the transactions. Those lines have amounts but aren't
    transactions; they used to send the whole statement to AI."""
    pdf = _make_statement_pdf([
        "Customer service: 1.800.432.1000",
        "for September 5, 2026 to October 6, 2026",
        "Beginning balance on September 5, 2026 $326.44",
        "Deposits and other additions 100.00",
        "Withdrawals and other subtractions -183.59",
        "Ending balance on October 6, 2026 $242.85",
        "09/08/26 Zelle payment from A FRIEND Conf# 99cvw5uk2 100.00",
        "Total deposits and other additions $100.00",
        "09/10/26 MOBILE PURCHASE 0909 STARBUCKS STORE 51971 CORONA CA -5.95",
        "09/22/26 TEXAS ROADHOUSE #2447 CORONA CA -177.64",
        "Total withdrawals and other subtractions -$183.59",
    ])
    with patch("artha.services.ai_service.AIService.extract_pdf_transactions") as extract:
        resp = _upload_pdf(auth_client, pdf)
    assert resp.status_code == 200
    body = resp.get_json()
    assert not extract.called
    assert body["totals"] == "match"
    assert len(body["rows"]) == 3
    assert _row(body["rows"], "starbucks")["description"] == "Starbucks Store Corona CA"


def test_a_german_statement(auth_client):
    pdf = _make_statement_pdf([
        "Kontoauszug 09/2026 EUR",
        "Alter Kontostand 1.258,01",
        "05.09.2026 REWE Markt GmbH -23,45 1.234,56",
        "07.09.2026 Gehalt September +2.500,00 3.734,56",
        "Neuer Kontostand 3.734,56",
    ])
    resp = _upload_pdf(auth_client, pdf)
    body = resp.get_json()
    rewe, salary = _row(body["rows"], "rewe"), _row(body["rows"], "gehalt")
    assert (rewe["date"], rewe["amount"], rewe["type"]) == ("2026-09-05", 23.45, "expense")
    assert (salary["amount"], salary["type"]) == (2500.00, "income")
    assert body["totals"] == "match"


def test_a_uk_statement_with_unsigned_amounts_reads_direction_from_the_balance(auth_client):
    """UK, Indian and most European statements print money out and money in
    as separate columns; in the text the empty one disappears, leaving an
    unsigned amount and the running balance."""
    pdf = _make_statement_pdf([
        "Statement date 30 September 2026 GBP",
        "Balance brought forward 1,000.00",
        "05 Sep TESCO STORES 45.23 954.77",
        "06 Sep SALARY ACME LTD 1,500.00 2,454.77",
        "08 Sep TFL TRAVEL CH 6.80 2,447.97",
        "Balance carried forward 2,447.97",
    ])
    resp = _upload_pdf(auth_client, pdf)
    body = resp.get_json()
    assert _row(body["rows"], "tesco")["type"] == "expense"
    assert _row(body["rows"], "tesco")["date"] == "2026-09-05"
    assert _row(body["rows"], "salary")["type"] == "income"
    assert _row(body["rows"], "tfl")["type"] == "expense"
    assert body["totals"] == "match"


def test_an_indian_statement_with_dr_and_cr(auth_client):
    pdf = _make_statement_pdf([
        "Statement period 01/09/2026 to 30/09/2026 INR",
        "05/09/2026 UPI/SWIGGY/ORDER 450.00 Dr 12,345.00 Cr",
        "15/09/2026 NEFT SALARY ACME 50,000.00 Cr 62,345.00 Cr",
    ])
    resp = _upload_pdf(auth_client, pdf)
    rows = resp.get_json()["rows"]
    assert _row(rows, "swiggy")["type"] == "expense" and _row(rows, "swiggy")["amount"] == 450.00
    assert _row(rows, "salary")["type"] == "income"


def test_a_credit_card_statement_reads_purchases_as_spending(auth_client):
    """On a card statement an unsigned amount is a purchase and a negative
    one a payment; read like a bank statement, every purchase came in as
    income."""
    pdf = _make_statement_pdf([
        "Payment Due Date 10/25/2026 Minimum Payment Due $35.00",
        "Credit Limit $5,000.00",
        "Sep 05 AMAZON MKTPLACE PMTS 45.99",
        "Sep 10 PAYMENT THANK YOU -500.00",
        "Sep 12 UBER TRIP 18.20",
    ])
    resp = _upload_pdf(auth_client, pdf)
    rows = resp.get_json()["rows"]
    assert _row(rows, "amazon")["type"] == "expense"
    assert _row(rows, "uber")["type"] == "expense"
    assert _row(rows, "payment")["type"] == "income"


def test_ai_is_skipped_once_the_time_budget_is_spent(auth_client, monkeypatch):
    """Past the budget the import answers with what it has instead of
    running into the web server's 30-second limit."""
    from artha.blueprints.finance import routes
    monkeypatch.setattr(routes, "_IMPORT_TIME_BUDGET_SECONDS", 0.0)
    pdf = _make_statement_pdf([
        "05/06/2026 Opening line with no amount",
        "-45.00",
    ])
    with patch("artha.services.ai_service.AIService.extract_pdf_transactions") as extract:
        resp = _upload_pdf(auth_client, pdf)
    assert not extract.called
    assert resp.status_code == 400


def test_ai_reads_a_long_statement_a_few_pages_at_a_time_within_the_budget(auth_client):
    lines = []
    for page in range(6):
        lines += [f"Page {page + 1} of 6"] + [f"Item {page}-{i} with its amount printed on the next line, a layout no pattern fits" for i in range(40)] + ["-1.00"] * 10
    pdf = _make_statement_pdf(lines)
    with patch("artha.services.ai_service._get_client") as get_client:
        get_client.return_value.messages.create.return_value = _fake_tool_response([])
        _upload_pdf(auth_client, pdf)
        calls = get_client.return_value.messages.create.call_args_list
    assert len(calls) >= 2
    assert all(0 < c.kwargs["timeout"] <= 22 for c in calls)


# ---------------------------------------------------------------------------
# CSV, OFX, QIF
# ---------------------------------------------------------------------------

def test_a_european_csv_with_semicolons_and_account_details_above_the_header(auth_client):
    csv = (
        "Kontonummer;DE12 3456\n"
        "Zeitraum;01.09.2026 - 30.09.2026\n"
        "\n"
        "Buchungstag;Verwendungszweck;Betrag (EUR)\n"
        "05.09.2026;REWE Markt;-23,45\n"
        "07.09.2026;Gehalt;2.500,00\n"
    ).encode("utf-8")
    rows = _upload(auth_client, csv, "umsaetze.csv").get_json()["rows"]
    rewe = _row(rows, "rewe")
    assert (rewe["date"], rewe["amount"], rewe["type"]) == ("2026-09-05", 23.45, "expense")
    assert _row(rows, "gehalt")["amount"] == 2500.00


def test_a_csv_with_a_direction_column_and_a_currency_column(auth_client):
    csv = (
        "Transaction Date,Description,Amount (USD),Type,Currency\n"
        "2026-09-05 14:22:01,Apple Store,45.00,Purchase,USD\n"
        "2026-09-10 09:00:00,Payment received,500.00,Payment,USD\n"
        "2026-09-11 09:00:00,Cafe Paris,4.50,Purchase,EUR\n"
    ).encode()
    rows = _upload(auth_client, csv, "card.csv").get_json()["rows"]
    assert _row(rows, "apple store")["type"] == "expense"
    assert _row(rows, "payment")["type"] == "income"
    assert _row(rows, "cafe")["currency"] == "EUR"


def test_an_ofx_file(auth_client):
    ofx = b"""OFXHEADER:100
DATA:OFXSGML
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>GBP<BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260905120000<TRNAMT>-45.23<FITID>1<NAME>TESCO STORES 3297
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260906<TRNAMT>1500.00<FITID>2<NAME>ACME LTD<MEMO>SALARY
</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"""
    rows = _upload(auth_client, ofx, "statement.qfx").get_json()["rows"]
    tesco = _row(rows, "tesco")
    assert (tesco["date"], tesco["amount"], tesco["type"], tesco["currency"]) == ("2026-09-05", 45.23, "expense", "GBP")
    assert _row(rows, "acme")["type"] == "income"


def test_a_qif_file(auth_client):
    qif = b"!Type:Bank\nD09/05/2026\nT-12.50\nPCORNER CAFE\n^\nD09/06/2026\nT2,000.00\nPPAYROLL ACME\n^\n"
    rows = _upload(auth_client, qif, "export.qif").get_json()["rows"]
    assert _row(rows, "cafe")["type"] == "expense" and _row(rows, "cafe")["amount"] == 12.50
    assert _row(rows, "payroll")["date"] == "2026-09-06"


# ---------------------------------------------------------------------------
# Already in Artha, and transfers
# ---------------------------------------------------------------------------

def _add(user, description, amount, when, **extra):
    db.session.add(Transaction(
        user_id=user.id, description=description, amount=Decimal(amount), type="expense",
        timestamp=datetime.combine(when, datetime.min.time()), currency="USD", usd_value=Decimal(amount),
        **extra,
    ))
    db.session.commit()


def test_a_purchase_apple_pay_already_logged_starts_unticked(auth_client, user):
    _add(user, "Starbucks", "5.95", date(2026, 9, 9))
    pdf = _make_statement_pdf(["09/10/26 MOBILE PURCHASE 0909 STARBUCKS STORE 51971 CORONA CA -5.95"])
    rows = _upload_pdf(auth_client, pdf).get_json()["rows"]
    assert rows[0]["duplicate"] is True


def test_a_bill_waiting_for_its_charge_is_not_a_duplicate(auth_client, user):
    _add(user, "Netflix", "15.49", date(2026, 9, 6), is_recurring=True, recurrence_interval="monthly")
    pdf = _make_statement_pdf(["09/06/26 NETFLIX.COM LOS GATOS CA -15.49"])
    rows = _upload_pdf(auth_client, pdf).get_json()["rows"]
    assert rows[0]["duplicate"] is False


def test_the_app_gets_transfers_and_totals(client, user):
    from .test_api_import import _headers, _preview
    pdf = _make_statement_pdf([
        "Beginning balance on September 5, 2026 $100.00",
        "Ending balance on October 6, 2026 $50.00",
        "09/15/26 Online Banking transfer to SAV 7837 Confirmation# 5650106972 -50.00",
    ])
    body = _preview(client, _headers(client, user), pdf.read(), filename="s.pdf").get_json()
    assert body["rows"][0]["transfer"] is True
    assert body["totals"] == "match"
