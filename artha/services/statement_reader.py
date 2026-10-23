"""
The pieces of statement reading that don't depend on Flask: amounts,
dates and names written the way banks around the world write them, the
statement's own totals (to prove an import complete), and the two exact
export formats nearly every bank offers besides PDF and CSV (OFX/QFX and
QIF). blueprints/finance/routes.py's PDF and CSV parsers are built on
these; they're kept here so each rule has one copy and its own tests.

Ground rules, learned from real statements:
- An amount always has exactly two decimal places in a PDF line (that's
  what tells it apart from a store number, a date or a phone number), and
  whichever of "." or "," comes last before those two digits is the
  decimal point: 1,234.56 (US/UK/India), 1.234,56 (most of Europe and
  Latin America), 1'234.56 (Switzerland), 12,34,567.89 (India's lakh).
- A sign can be written five ways: -12.00, (12.00), 12.00-, 12.00 DR /
  12.00 CR, or not at all. When there's no sign, the running balance
  (if the statement prints one) says which way the money went.
- A credit card statement turns that around: an unsigned amount is a
  purchase, a negative or CR one is a payment or refund.
"""

import calendar
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

# ---------------------------------------------------------------------------
# Month names, in the languages statements most often arrive in
# ---------------------------------------------------------------------------

_MONTH_WORDS = {
    1: "january jan janvier janv enero ene januar jänner jän gennaio gen janeiro januari",
    2: "february feb février fevrier févr fevr fév fev febrero februar febbraio fevereiro februari",
    3: "march mar mars marzo märz maerz mär mrz março marco maart mrt",
    4: "april apr avril avr abril abr aprile",
    5: "may mai mayo maggio mag maio mei",
    6: "june jun juin junio juni giugno giu junho",
    7: "july jul juillet juil julio juli luglio lug julho",
    8: "august aug août aout agosto ago augustus",
    9: "september sep sept septembre septiembre setiembre set settembre setembro",
    10: "october oct octobre octubre oktober okt ottobre ott outubro out",
    11: "november nov novembre noviembre novembro",
    12: "december dec décembre decembre déc diciembre dic dezember dez dicembre dezembro",
}
MONTHS = {word: num for num, words in _MONTH_WORDS.items() for word in words.split()}
_MONTH_ALT = "|".join(sorted((re.escape(w) for w in MONTHS), key=len, reverse=True))

# ---------------------------------------------------------------------------
# Amounts
# ---------------------------------------------------------------------------

_CURRENCY_SYMBOLS = "$£€৳₹¥₩₱₦₺₽₪฿₫"
_CURRENCY_CODES = (
    "USD|EUR|GBP|INR|BDT|CAD|AUD|NZD|JPY|CNY|CHF|SEK|NOK|DKK|SGD|HKD|AED|SAR|QAR|KWD|"
    "PKR|LKR|NPR|ZAR|NGN|KES|EGP|MXN|BRL|ARS|COP|CLP|PLN|CZK|HUF|RON|TRY|PHP|MYR|THB|IDR|VND|KRW"
)
_AMOUNT_CORE = r"(?:\d{1,3}(?:[,.'  ]\d{3})+|\d+)[.,]\d{2}"
# One amount as a statement line writes it. Never starts inside a longer
# number, and never ends with a third decimal digit, so a phone number
# ("1.800.432.1000") or a reference ("5265694029") isn't mistaken for money.
MONEY = (
    rf"(?<![\w.,])(?:[-−–+]\s?)?\(?(?:[{_CURRENCY_SYMBOLS}]|(?:{_CURRENCY_CODES})\s?)?\s?"
    rf"[-−]?{_AMOUNT_CORE}(?!\d)\)?(?:-|\s?(?:CR|DR|Cr|Dr)\b)?"
)
_MONEY_RE = re.compile(MONEY)


def parse_amount(raw: str) -> tuple[Decimal | None, bool]:
    """(signed amount, whether the text said which way it went). A bare
    "12.00" is (12.00, False): a deposit on a bank statement, a purchase on
    a card statement, or whatever the running balance shows; the caller
    decides. "-12.00", "(12.00)", "12.00-" and "12.00 DR" are negative;
    "+12.00" and "12.00 CR" positive, all with explicit=True."""
    s = (raw or "").strip()
    if not s:
        return None, False
    negative = explicit = False
    marker = re.search(r"\s?(CR|DR)\.?$", s, re.IGNORECASE)
    if marker:
        negative = marker.group(1).upper() == "DR"
        explicit = True
        s = s[: marker.start()].strip()
    if s.endswith("-"):
        negative = explicit = True
        s = s[:-1].strip()
    if s.startswith("(") and s.endswith(")"):
        negative = explicit = True
        s = s[1:-1].strip()
    s = re.sub(rf"^(?:{_CURRENCY_CODES})\s?|\s?(?:{_CURRENCY_CODES})$", "", s)
    s = s.strip(_CURRENCY_SYMBOLS + "  ")
    if s[:1] in "-−–":
        negative = explicit = True
        s = s[1:].strip()
    elif s[:1] == "+":
        explicit = True
        s = s[1:].strip()
    s = s.strip(_CURRENCY_SYMBOLS + "  ")
    if s.startswith("(") and s.endswith(")"):
        negative = explicit = True
        s = s[1:-1]
    s = s.replace(" ", "").replace(" ", "").replace("'", "").replace(" ", "")
    if not s or not re.fullmatch(r"[\d.,]*\d", s):
        return None, False

    last = max(s.rfind("."), s.rfind(","))
    if last == -1:
        number = s
    else:
        decimals = len(s) - last - 1
        sep = s[last]
        other = "," if sep == "." else "."
        if decimals in (1, 2) or (decimals == 3 and (other in s or (sep == "." and s.count(".") == 1))):
            # The last separator is the decimal point: "12.5", "1.234,56",
            # "1,234.567" (a three-decimal currency like KWD).
            number = s[:last].replace(".", "").replace(",", "") + "." + s[last + 1:]
        else:
            # Only thousands separators: "1,234" or "1.234.567".
            number = s.replace(".", "").replace(",", "")
    try:
        value = Decimal(number)
    except InvalidOperation:
        return None, False
    return (-value if negative else value), explicit


def amounts_in(line: str) -> list[str]:
    return [m.group(0).strip() for m in _MONEY_RE.finditer(line)]


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

# One date as statements print it, at the start of a line. Each needs a
# separator or a month name, so a store number can't pass for one.
DATE = (
    r"(?:"
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"                                   # 2026-09-05, 2026/09/05
    r"|\d{4}年\d{1,2}月\d{1,2}日|\d{1,2}月\d{1,2}日"                      # 2026年9月5日
    r"|\d{1,2}\.\d{1,2}\.(?:\d{4}|\d{2}(?!\d))?"                         # 05.09.2026, 05.09.26, 05.09.
    r"|\d{1,2}/\d{1,2}(?:/(?:\d{4}|\d{2}(?!\d)))?"                       # 09/05/2026, 09/05
    r"|\d{1,2}-\d{1,2}-(?:\d{4}|\d{2}(?!\d))"                            # 05-09-2026
    rf"|\d{{1,2}}(?:st|nd|rd|th)?[ \-.]?(?:{_MONTH_ALT})\.?(?:[-/']\d{{2}}(?!\d)|[ \-,.]+\d{{4}})?(?![A-Za-zÀ-ÿ])"  # 05 Sep 2026, 5-Sep-26, 05SEP
    rf"|(?:{_MONTH_ALT})\.?[ \-]\d{{1,2}}(?!\d)(?:,?\s\d{{4}})?"          # Sep 5, Sep 05, 2026
    r")"
)
_DATE_RE = re.compile(DATE, re.IGNORECASE)
_DATE_AT_START_RE = re.compile(rf"^(?:\d+\s+)?{DATE}(?=\s)", re.IGNORECASE)


# A date cut short by a page break ("05/08/2" on one page, "026" on the
# next) still starts a transaction line.
_PARTIAL_DATE_AT_START_RE = re.compile(r"^(?:\d+\s+)?\d{1,4}[/.\-]\d")


def starts_with_date(line: str) -> bool:
    return bool(_DATE_AT_START_RE.match(line) or _PARTIAL_DATE_AT_START_RE.match(line))


def _year(two_or_four: str) -> int:
    y = int(two_or_four)
    if y < 100:
        y += 2000 if y < 70 else 1900
    return y


def _infer_year(month: int, day: int, anchor: date | None) -> date | None:
    """A date printed without a year belongs to the statement it's in: the
    year that puts it on or shortly before the statement's last day (a
    December purchase on a January statement is last year's)."""
    ref = anchor or date.today()
    for year in (ref.year, ref.year - 1, ref.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate <= ref + timedelta(days=45) and candidate > ref - timedelta(days=320):
            return candidate
    try:
        return date(ref.year, month, day)
    except ValueError:
        return None


def parse_date(token: str, day_first: bool = False, anchor: date | None = None) -> date | None:
    """Reads one date token from a statement. A numeric date with both
    parts 12 or under (03/04) follows `day_first` (one answer for the whole
    statement, see the PDF parser), a dotted one (03.04.2026) is always
    day-first, and a missing year comes from `anchor`, the statement's end."""
    s = (token or "").strip().rstrip(".,")
    if not s:
        return None
    s = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", s, flags=re.IGNORECASE)

    m = re.fullmatch(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日", s)
    if m:
        y, mo, d = m.groups()
        if y:
            return _safe_date(int(y), int(mo), int(d))
        return _infer_year(int(mo), int(d), anchor)

    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    m = re.fullmatch(r"(\d{1,2})([/.\-])(\d{1,2})(?:\2(\d{2}|\d{4}))?\.?", s)
    if m:
        a, sep, b, y = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4)
        if a > 12 and b <= 12:
            day, month = a, b
        elif b > 12 and a <= 12:
            month, day = a, b
        elif sep == "." or day_first:
            day, month = a, b
        else:
            month, day = a, b
        if y:
            return _safe_date(_year(y), month, day)
        return _infer_year(month, day, anchor)

    m = re.fullmatch(rf"(\d{{1,2}})[ \-.]?({_MONTH_ALT})\.?(?:[-/' ,.]+(\d{{4}}|\d{{2}}))?", s, re.IGNORECASE)
    if m:
        day, month, y = int(m.group(1)), MONTHS[m.group(2).lower()], m.group(3)
        return _safe_date(_year(y), month, day) if y else _infer_year(month, day, anchor)

    m = re.fullmatch(rf"({_MONTH_ALT})\.?[ \-](\d{{1,2}})(?:,?\s(\d{{4}}))?", s, re.IGNORECASE)
    if m:
        month, day, y = MONTHS[m.group(1).lower()], int(m.group(2)), m.group(3)
        return _safe_date(int(y), month, day) if y else _infer_year(month, day, anchor)

    m = re.fullmatch(rf"({_MONTH_ALT})\.?\s(\d{{1,2}}),?\s(\d{{4}})", s, re.IGNORECASE)
    if m:
        return _safe_date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))
    return None


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


_FULL_MONTH_DATE_RE = re.compile(
    rf"\b(?:\d{{1,2}}\s(?:{_MONTH_ALT})\.?,?\s\d{{4}}|(?:{_MONTH_ALT})\.?\s\d{{1,2}},?\s\d{{4}})\b",
    re.IGNORECASE,
)
_FULL_NUMERIC_DATE_RE = re.compile(r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b|\b\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4}\b")


def statement_end_date(text: str, day_first: bool = False) -> date | None:
    """The latest full date (with a year) the statement mentions that isn't
    in the future: the end of its period on every statement seen, used to
    give a year to dates printed without one ("09/08", "05 Sep")."""
    today = date.today() + timedelta(days=7)
    found = []
    for m in _FULL_MONTH_DATE_RE.finditer(text):
        d = parse_date(m.group(0).replace(",", " ").replace("  ", " "), day_first)
        if d is None:
            d = _parse_month_first_full(m.group(0))
        if d:
            found.append(d)
    for m in _FULL_NUMERIC_DATE_RE.finditer(text):
        d = parse_date(m.group(0), day_first)
        if d:
            found.append(d)
    found = [d for d in found if date(1990, 1, 1) <= d <= today]
    return max(found) if found else None


def _parse_month_first_full(s: str) -> date | None:
    m = re.fullmatch(rf"({_MONTH_ALT})\.?\s(\d{{1,2}}),?\s(\d{{4}})", s.strip(), re.IGNORECASE)
    if not m:
        return None
    return _safe_date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))


# ---------------------------------------------------------------------------
# What kind of statement, and does it add up
# ---------------------------------------------------------------------------

_CARD_SIGNALS = (
    "minimum payment", "payment due date", "credit limit", "available credit",
    "credit card statement", "new balance", "statement closing date",
    "mindestzahlung", "kreditkarte", "pago mínimo", "paiement minimum", "carte de crédit",
)


def is_credit_card_statement(text: str) -> bool:
    lower = text.lower()
    return sum(signal in lower for signal in _CARD_SIGNALS) >= 2


_OPENING_LABEL = (
    r"(?:beginning|opening|previous|starting|prior)\s+balance|balance\s+(?:brought\s+forward|b/?f)|"
    r"brought\s+forward|saldo\s+(?:anterior|inicial|iniziale|precedente|vorig)|anfangssaldo|"
    r"alter\s+(?:konto)?stand|ancien\s+solde|solde\s+(?:pr[ée]c[ée]dent|initial|de\s+d[ée]but)|beginsaldo"
)
_CLOSING_LABEL = (
    r"(?:ending|closing|current)\s+balance|new\s+balance|balance\s+(?:carried\s+forward|c/?f)|"
    r"carried\s+forward|saldo\s+(?:final|actual|nuevo|finale|attuale|nieuw)|endsaldo|"
    r"neuer\s+(?:konto)?stand|nouveau\s+solde|solde\s+(?:final|de\s+fin|nouveau)|eindsaldo"
)
_MONEY_IN_TOTAL = (
    r"total\s+(?:deposits|credits|money\s+in|paid\s+in|receipts|additions)|"
    r"(?:deposits|credits)\s+(?:and\s+other\s+)?(?:additions|credits)\s+total|summe\s+haben|total\s+abonos|total\s+cr[ée]dits?"
)
_MONEY_OUT_TOTAL = (
    r"total\s+(?:withdrawals|debits|money\s+out|paid\s+out|payments\s+and\s+other|subtractions)|"
    r"summe\s+soll|total\s+cargos|total\s+d[ée]bits?"
)


def _labelled_amount(text: str, label: str) -> Decimal | None:
    for line in text.split("\n"):
        if re.search(label, line, re.IGNORECASE):
            found = amounts_in(line)
            if found:
                value, _ = parse_amount(found[-1])
                if value is not None:
                    return value
    return None


def opening_balance(text: str) -> Decimal | None:
    return _labelled_amount(text, _OPENING_LABEL)


def check_totals(text: str, rows: list[dict], card: bool = False) -> str | None:
    """Proves the rows complete against the statement's own numbers:
    "match" when the opening balance plus what came in, less what went
    out, is the closing balance (or the money-in and money-out totals both
    agree), "mismatch" when the statement has those numbers and the rows
    don't add up to them, None when it doesn't print them."""
    if not rows:
        return None
    money_in = sum(Decimal(str(r["amount"])) for r in rows if r["type"] == "income")
    money_out = sum(Decimal(str(r["amount"])) for r in rows if r["type"] == "expense")
    cent = Decimal("0.01")

    opening = _labelled_amount(text, _OPENING_LABEL)
    closing = _labelled_amount(text, _CLOSING_LABEL)
    if opening is not None and closing is not None:
        change = (money_out - money_in) if card else (money_in - money_out)
        # Card statements print what's owed; some write it negative.
        if abs(abs(opening) + change - abs(closing)) < cent or abs(opening + change - closing) < cent:
            return "match"

    total_in = _labelled_amount(text, _MONEY_IN_TOTAL)
    total_out = _labelled_amount(text, _MONEY_OUT_TOTAL)
    if total_in is not None and total_out is not None:
        if abs(abs(total_in) - money_in) < cent and abs(abs(total_out) - money_out) < cent:
            return "match"
        return "mismatch"
    if opening is not None and closing is not None:
        return "mismatch"
    return None


def resolve_signs_from_balances(entries: list[dict], opening: Decimal | None) -> None:
    """For lines that printed an amount and a running balance but no sign
    (most UK, Indian and European statements, where money out and money in
    are separate columns and the empty one vanishes from the text), the
    balance moving up or down says which it was. Mutates each entry's
    "type" when the balance proves it; leaves the rest alone."""
    if len(entries) < 1:
        return
    ordered = list(entries)
    dates = [e["date"] for e in ordered]
    if dates != sorted(dates) and dates == sorted(dates, reverse=True):
        ordered.reverse()  # newest first, as some banks print it
        opening = None
    previous = opening
    for e in ordered:
        balance = e.get("balance")
        if balance is None:
            previous = None
            continue
        if previous is not None and not e.get("explicit"):
            change = balance - previous
            if abs(abs(change) - abs(e["value"])) < Decimal("0.01") and change != 0:
                e["type"] = "income" if change > 0 else "expense"
        previous = balance


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

_CARD_VERBS = (
    r"(?:(?:MOBILE|ONLINE|RECURRING|INTL|INTERNATIONAL|CONTACTLESS)\s+)?"
    r"(?:PURCHASE(?:\s+RETURN)?|CHECKCARD|CHECK\s+CARD|DEBIT\s+CARD\s+PURCHASE|DEBIT\s+PURCHASE|CARD\s+PURCHASE|"
    r"VISA\s+PURCHASE|POS\s+PURCHASE|POS\s+DEBIT|POS|PMNT\s+SENT|PMNT\s+RCVD|CARD\s+PAYMENT|VISA-POS|VISA\s+DEBIT)"
)
_KEEP_UPPER = {"ATM", "USA", "UK", "US", "EU", "LLC", "INC", "LTD", "ACH", "ID", "TV", "BP", "CVS", "AT&T", "IRS", "USPS", "UPS", "DHL", "KFC", "H&M", "IKEA", "HBO", "AMC"}
_SMALL_WORDS = {"of", "and", "the", "for", "to", "from", "at", "in", "on", "de", "la", "le", "du", "des", "y", "und"}


def clean_description(raw: str) -> str:
    """A bank's line ("MOBILE PURCHASE 0909 STARBUCKS STORE 51971 CORONA CA")
    as a person would name it ("Starbucks Store Corona CA"): card and
    transfer boilerplate, reference numbers, card masks, phone numbers and
    in-line dates removed, repeated words once, and shouting turned into
    title case. Conservative: if cleaning would leave nothing readable,
    the original comes back unchanged."""
    s = re.sub(r"\s+", " ", raw or "").strip()
    if not s:
        return raw or ""

    # US ACH: "PENNYMAC DES:CASH ID:8204... INDN:... CO ID:... PPD".
    ach = re.match(r"^(?P<co>.+?)\s+DES:\s?(?P<des>.+?)(?:\s+(?:ID|INDN|CO\s?ID):.*)?$", s)
    if ach:
        # "FIDELITY 34061 P": the account number and a lone letter go.
        co = re.sub(r"(?:\s+(?:\d+|[A-Z]))+$", "", ach.group("co").strip())
        des = ach.group("des").strip()
        s = co if des.upper() in {"PAYMENT", "PAYMENTS", "ACH", "DEBIT", "CREDIT", "PMT", "CASH", "PAYROLL", "DIR DEP"} else f"{co} {des}"
        if ach.group("des").strip().upper() in {"PAYROLL", "DIR DEP"}:
            s = f"{co} Payroll"

    s = re.sub(rf"^{_CARD_VERBS}(?:\s+\d{{4}})?\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(?:MOBILE\s+)?PURCHASE\b(?=.*\S)", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(?:Confirmation|Conf|Ref|Reference|Trace|Txn|Auth)\s*(?:#|No\.?|:)\s*\S+", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", " ", s)
    s = re.sub(r"#\s?\d+", " ", s)
    s = re.sub(r"\b(?:1[-.\s]?)?\d{3}[-.]\d{3}[-.]?\d{4}\b", " ", s)
    s = re.sub(r"\b[X*]{3,}\d{0,6}\b|\*\d{4}\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\b[A-Za-z]{0,2}\d{8,}\b", " ", s)
    s = re.sub(r"(?<![\w&-])\d{4,7}(?![\w&-])", " ", s)
    s = " ".join(_drop_codes(word) for word in s.split())
    s = re.sub(r"\s+", " ", s).strip(" -*,;:/")
    s = _split_glued_state(s)

    words, seen = [], set()
    for word in s.split(" "):
        key = word.upper()
        if len(word) >= 4 and key in seen:
            continue
        seen.add(key)
        words.append(word)
    s = " ".join(words)

    original = re.sub(r"\s+", " ", raw or "").strip()
    # Nothing left but generic words ("VISA-POS purchase" down to
    # "purchase"): the bank's own wording says more.
    if not s or not re.search(r"[A-Za-zÀ-ÿ]{2}", s) or not name_tokens(s):
        return original

    letters = [c for c in s if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        s = _title(s)
    return s[:120]


def _drop_codes(word: str) -> str:
    """Reference codes inside a name: "OIL12867495025" keeps "OIL",
    "STATERBROS170" keeps "STATERBROS", and "CLFTJNEL4D", "518598PN0" or
    "o-y9ibjqo" (letters and digits mixed through) go entirely. Works on
    each "*"-separated part, so "PP*SPOTIFY*P477F06456" keeps "PP*SPOTIFY"."""
    parts = word.split("*")
    kept = []
    for part in parts:
        trailing = re.fullmatch(r"([A-Za-z][A-Za-z.&'-]{2,})(\d{3,})", part)
        if trailing:
            kept.append(trailing.group(1))
        elif len(part) >= 7 and re.search(r"[A-Za-z]\d|\d[A-Za-z]", part):
            continue
        else:
            kept.append(part)
    return "*".join(kept) if any(kept) else ""


_US_STATES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND "
    "OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)


def _split_glued_state(s: str) -> str:
    """Card lines are fixed width, so a long city runs into its state:
    "Rancho CucamoCA", "WEST HOLLYWOOCA", "Amzn.com/billWA". In mixed case
    the switch from lower to upper gives it away; in shouting caps only a
    long last word of a three-word-or-longer name is split, so "COSTCO",
    "TACO" or "VENMO CASHOUT" never are."""
    words = s.split(" ")
    if len(words) < 2:
        return s
    last = words[-1]
    if last in _US_STATES:
        return s
    mixed = re.fullmatch(r"(.*[a-z]{3})([A-Z]{2})", last)
    if mixed and mixed.group(2) in _US_STATES:
        words[-1] = mixed.group(1) + " " + mixed.group(2)
    elif (last == last.upper() and len(words) >= 3 and len(last) >= 7 and last[-2:] in _US_STATES
          and (last[-3].isalpha() or last[-3] in "/.")):
        words[-1] = last[:-2] + " " + last[-2:]
    return " ".join(words)


def _title(s: str) -> str:
    out = []
    words = s.split(" ")
    for i, word in enumerate(words):
        bare = word.strip(".,*")
        if bare.upper() in _KEEP_UPPER or (len(bare) == 2 and bare.isalpha() and i == len(words) - 1):
            out.append(word.upper())
        elif i > 0 and bare.lower() in _SMALL_WORDS:
            out.append(word.lower())
        elif any(c.isdigit() for c in word):
            out.append(word.upper() if len(word) <= 4 else word.capitalize())
        else:
            out.append(word[:1].upper() + word[1:].lower())
    # "Google *Chrome", "Apple.com/Bill", "Pp*Spotify".
    return re.sub(r"([*/])([a-z])", lambda m: m.group(1) + m.group(2).upper(), " ".join(out))


# ---------------------------------------------------------------------------
# OFX / QFX and QIF: exact exports, no guessing needed
# ---------------------------------------------------------------------------

def looks_like_ofx(head: bytes) -> bool:
    sample = head[:4000].upper()
    return b"OFXHEADER" in sample or b"<OFX>" in sample


def looks_like_qif(head: bytes) -> bool:
    return head.lstrip()[:6].upper() == b"!TYPE:" or head.lstrip()[:8].upper() == b"!OPTION:"


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def parse_ofx(data: bytes, fallback_currency: str) -> tuple[list[dict], list[str], str]:
    """OFX 1.x (SGML, tags often left unclosed) and 2.x (XML) alike, and
    QFX, which is OFX with Quicken's header. Amounts are already signed
    from the account holder's side, for bank and card accounts both."""
    text = _decode(data)
    currency_m = re.search(r"<CURDEF>\s*([A-Za-z]{3})", text)
    currency = currency_m.group(1).upper() if currency_m else fallback_currency
    rows, warnings, seen_ids = [], [], set()
    blocks = re.findall(r"<STMTTRN>(.*?)(?=</STMTTRN>|<STMTTRN>|</BANKTRANLIST>|</CCSTMTRS>|\Z)", text, re.S | re.I)
    for block in blocks:
        def tag(name):
            m = re.search(rf"<{name}>([^<\r\n]*)", block, re.I)
            return m.group(1).strip() if m else ""

        fitid = tag("FITID")
        if fitid and fitid in seen_ids:
            continue
        seen_ids.add(fitid)
        raw_date = tag("DTPOSTED") or tag("DTUSER")
        try:
            when = datetime.strptime(raw_date[:8], "%Y%m%d").date()
        except ValueError:
            warnings.append("Skipped a transaction with no readable date.")
            continue
        value, _ = parse_amount(tag("TRNAMT"))
        if value is None or value == 0:
            continue
        name, memo = _unescape(tag("NAME")), _unescape(tag("MEMO"))
        description = name or memo or tag("TRNTYPE").title()
        if name and memo and len(name) < 12 and memo.lower() not in name.lower():
            description = f"{name} {memo}"
        rows.append({
            "date": when.isoformat(),
            "description": description,
            "amount": float(abs(value)),
            "type": "income" if value > 0 else "expense",
            "currency": currency,
        })
    if not blocks:
        warnings.append("That OFX file has no transactions in it.")
    return rows, warnings, currency


def _unescape(s: str) -> str:
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&apos;", "'").replace("&quot;", '"')


def parse_qif(data: bytes, fallback_currency: str) -> tuple[list[dict], list[str]]:
    """QIF from a bank or card export: records of D(ate), T or U (amount,
    signed from the account holder's side), P(ayee), M(emo), ended by ^."""
    text = _decode(data)
    records, current = [], {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("!"):
            continue
        if line == "^":
            if current:
                records.append(current)
            current = {}
            continue
        code, value = line[0], line[1:].strip()
        if code in "DTUPM" and code not in current:
            current[code] = value
    if current:
        records.append(current)

    dates = " ".join(r.get("D", "").replace("'", "/") for r in records)
    day_first = _numeric_dates_day_first(dates)
    rows, warnings = [], []
    for r in records:
        when = parse_date(r.get("D", "").replace("'", "/").replace(" ", ""), day_first)
        value, _ = parse_amount(r.get("T") or r.get("U") or "")
        if when is None or value is None:
            warnings.append("Skipped a transaction with no readable date or amount.")
            continue
        if value == 0:
            continue
        rows.append({
            "date": when.isoformat(),
            "description": r.get("P") or r.get("M") or "Transaction",
            "amount": float(abs(value)),
            "type": "income" if value > 0 else "expense",
            "currency": fallback_currency,
        })
    return rows, warnings


def _numeric_dates_day_first(text: str) -> bool:
    for a, b in re.findall(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-]\d{2,4}\b", text):
        if int(a) > 12:
            return True
        if int(b) > 12:
            return False
    return False


# ---------------------------------------------------------------------------
# Money moving between the person's own accounts
# ---------------------------------------------------------------------------

_OWN_TRANSFER_RE = re.compile(
    r"\b(?:online|mobile|internet)?\s*banking\s+transfer\s+(?:to|from)\b"
    r"|\btransfer\s+(?:to|from)\s+(?:sav|savings|chk|chkg|checking|share|acct|account|x+\d*|\*+\d+)\b"
    r"|\bxfer\s+(?:to|from)\s+(?:sav|chk|savings|checking)\b"
    r"|\binternal\s+transfer\b|\bown\s+account\b|\bbetween\s+(?:your\s+|my\s+)?accounts\b"
    r"|\bcash\s?out\b|\bbank\s+xfer\b|\bsweep\s+(?:to|from)\b"
    r"|\bumbuchung\b|\bübertrag\b|\btraspaso\b|\bvirement\s+(?:interne|entre\s+(?:vos|mes)\s+comptes)\b"
    r"|\bgiroconto\b|\beigen\s+rekening\b",
    re.IGNORECASE,
)


# An account named on a statement line: a word that says it's an account
# (savings, checking, A/C) and its number. Never a bare card mask ("XXXX1234",
# "*1234"): that's on every card purchase, so learning it would leave the
# person's shopping out of every import.
_ACCOUNT_REF_RE = re.compile(
    r"\b(SAVINGS|SAV|CHECKING|CHK|CHQ|CHEQUING|ACCOUNT|ACCT|A/C|MONEY\s+MARKET|MMA|ISA|FD|RD)"
    r"\s*(?:NO\.?|#|ENDING(?:\s+IN)?)?\s*[X*]*\s*(\d{3,})\b",
    re.IGNORECASE,
)


def account_reference(text: str) -> tuple[str, str] | None:
    """(key, label) for the account a statement line names, like
    ("sav 7837", "SAV 7837"), or None. The last four digits are kept, so
    "SAV XXXXXX7837" and "SAV 7837" are the same account."""
    m = _ACCOUNT_REF_RE.search(text or "")
    if not m:
        return None
    word = re.sub(r"\s+", " ", m.group(1)).upper()
    label = f"{word} {m.group(2)[-4:]}"
    return label.lower(), label


def is_own_transfer(bank_text: str, name: str, first: str | None, last: str | None) -> bool:
    """Money moved between the person's own accounts (to savings, a Venmo
    or Apple Cash cash-out, a transfer in their own name) isn't spending
    or income; counted, it doubles both. The review starts these
    unticked rather than dropping them, so a person can still keep one."""
    if _OWN_TRANSFER_RE.search(bank_text or "") or _OWN_TRANSFER_RE.search(name or ""):
        return True
    if first and last and len(first) > 1 and len(last) > 1:
        lowered = (name or "").lower()
        if (re.search(rf"\b{re.escape(first.lower())}\b", lowered) and re.search(rf"\b{re.escape(last.lower())}\b", lowered)
                and re.search(r"\b(?:transfer|xfer|to self)\b", lowered)):
            return True
    return False


# ---------------------------------------------------------------------------
# Is this already in Artha?
# ---------------------------------------------------------------------------

_NAME_NOISE = {
    "purchase", "mobile", "checkcard", "payment", "pos", "debit", "credit", "card", "the", "store",
    "online", "transfer", "from", "inc", "com", "www", "sent", "recurring", "visa", "mastercard",
    "pmnt", "llc", "ltd", "and", "bill", "des", "indn", "web", "ppd", "apple", "pay",
}


def name_tokens(name: str) -> set[str]:
    words = re.findall(r"[a-zà-ÿ]{3,}", (name or "").lower())
    return {w for w in words if w not in _NAME_NOISE}


def names_match(a: str, b: str) -> bool:
    """Loose: two names for the same purchase ("Starbucks" logged with
    Apple Pay, "MOBILE PURCHASE 0909 STARBUCKS STORE 51971" on the
    statement) share a real word, or one squashed name contains the other."""
    ta, tb = name_tokens(a), name_tokens(b)
    if ta & tb:
        return True
    sa = re.sub(r"[^a-z]", "", (a or "").lower())
    sb = re.sub(r"[^a-z]", "", (b or "").lower())
    return len(sa) >= 4 and len(sb) >= 4 and (sa in sb or sb in sa)
