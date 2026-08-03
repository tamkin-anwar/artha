import re
from datetime import date, datetime
from decimal import Decimal
from html.parser import HTMLParser

from flask import request


def is_ajax_request() -> bool:
    """True when the request expects a JSON response rather than a full page."""
    xrw = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    accept_json = "application/json" in (request.headers.get("Accept") or "")
    return xrw or accept_json or request.path.startswith("/api/")


# ---------------------------------------------------------------------------
# Note title/preview derivation
#
# Note.content holds two different shapes of data: real HTML (innerHTML from
# the contenteditable rich-text editor) for notes saved by the current
# editor, and legacy markdown-ish plain text (e.g. "# Heading", "- item")
# for notes saved before it existed. This mirrors the client-side
# looksLikeHtml()/htmlToPlainText() pair in notes.html so both shapes are
# handled the same way server-side.
# ---------------------------------------------------------------------------

_LOOKS_LIKE_HTML_RE = re.compile(r"<[a-zA-Z][\s\S]*>")
_BLOCK_TAGS = {"div", "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "br"}


class _PlainTextExtractor(HTMLParser):
    """Strips tags and decodes entities, inserting a newline around each
    block-level element so line-based splitting (e.g. "first line" for a
    title) means something once the tags are gone."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def looks_like_html(text: str) -> bool:
    return bool(_LOOKS_LIKE_HTML_RE.search(text or ""))


def html_to_plain_text(raw: str) -> str:
    """Normalize note content (HTML or legacy plain text) into plain text
    with block boundaries collapsed to single newlines. Legacy plain text
    is passed through untouched rather than fed to the HTML parser, since
    it may contain literal '<'/'>' that aren't real tags."""
    if not raw:
        return ""

    if looks_like_html(raw):
        parser = _PlainTextExtractor()
        parser.feed(raw)
        parser.close()
        text = parser.get_text()
    else:
        text = raw

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _truncate(text: str, length: int, suffix: str = "…") -> str:
    """Word-boundary-aware truncation (mirrors Jinja's `truncate` filter
    default behavior) so we don't cut a word in half."""
    text = text.strip()
    if len(text) <= length:
        return text
    truncated = text[:length].rsplit(" ", 1)[0]
    if not truncated:
        truncated = text[:length]
    return truncated + suffix


def derive_title_and_preview(content: str):
    """Compute the auto-title (first line, short) and preview (flattened
    excerpt, longer) for a note's content. Returns (title_or_None, preview).

    title is None when content has no text at all — the caller/template
    falls back to "Untitled" in that case. When the user has typed an
    explicit title, callers should prefer that over this derived one and
    only fall back to it when the title field is blank.
    """
    plain = html_to_plain_text(content)
    if not plain:
        return None, ""

    first_line = plain.split("\n", 1)[0].strip()
    title = _truncate(first_line, 80) if first_line else None

    flat = re.sub(r"\s+", " ", plain).strip()
    preview = _truncate(flat, 160)

    return title, preview


def current_month_bounds() -> tuple[datetime, datetime]:
    """Return (start, end) datetimes bounding the current calendar month,
    for `Transaction.timestamp >= start, Transaction.timestamp < end`
    filtering. Shared by the dashboard and /api/finance_totals so both
    default to "this month" the same way the /finance page's month tabs
    already do (see _month_start in finance/routes.py)."""
    today = date.today()
    start = datetime(today.year, today.month, 1)
    if today.month == 12:
        end = datetime(today.year + 1, 1, 1)
    else:
        end = datetime(today.year, today.month + 1, 1)
    return start, end


def budget_status(cap: Decimal | None, spent: Decimal) -> dict:
    """
    Shared by the dashboard (alert banner) and Finance page (progress
    card) so both agree on the exact same thresholds. `cap` is None or
    <= 0 when the user hasn't set a budget yet — has_budget=False lets
    both callers skip rendering anything rather than showing a 0/$0 cap.

    tier: "ok" under 90%, "warning" 90-99%, "over" 100%+. Fixed rather
    than user-configurable — one less setting for a feature this small.
    """
    if not cap or cap <= 0:
        return {"has_budget": False}

    pct = float(spent / cap * 100)
    if pct >= 100:
        tier = "over"
    elif pct >= 90:
        tier = "warning"
    else:
        tier = "ok"

    return {
        "has_budget": True,
        "cap": float(cap),
        "spent": float(spent),
        "pct": pct,
        "pct_clamped": min(100.0, max(0.0, pct)),
        "tier": tier,
    }
