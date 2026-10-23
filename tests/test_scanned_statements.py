"""Scanned statements and photos (finance.routes._read_scanned_statement):
no text to parse, so AI reads the page images, with the same totals check
as any statement. AI is mocked; tests never call the real API."""

import io
from unittest.mock import patch

from PIL import Image
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


def _photo(fmt="JPEG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2400, 3200), "white").save(buffer, fmt)
    return buffer.getvalue()


def _scanned_pdf(pages=3) -> bytes:
    """A PDF that's only pictures of pages, like a scanner makes."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    for _ in range(pages):
        c.drawImage(ImageReader(io.BytesIO(_photo())), 0, 0, width=612, height=792)
        c.showPage()
    c.save()
    return buffer.getvalue()


def _upload(client, body, filename):
    return client.post("/finance/import/preview", data={"statement": (io.BytesIO(body), filename)},
                       content_type="multipart/form-data")


PAGE_ONE = {
    "transactions": [
        {"date": "2026-09-05", "description": "TESCO STORES", "amount": 45.23, "type": "expense"},
        {"date": "2026-09-06", "description": "SALARY ACME LTD", "amount": 1500.00, "type": "income"},
    ],
    "opening_balance": 1000.00, "currency": "GBP",
}
PAGE_TWO = {
    "transactions": [{"date": "2026-09-08", "description": "TFL TRAVEL", "amount": 6.80, "type": "expense"}],
    "closing_balance": 2447.97,
}


def test_a_scanned_pdf_is_read_from_its_pages_and_checked_against_its_totals(auth_client):
    calls = []

    def read(images, context="", timeout=None):
        calls.append((len(images), timeout))
        return PAGE_ONE if len(calls) == 1 else PAGE_TWO

    with patch("artha.services.ai_service.AIService.extract_scanned_statement", side_effect=read):
        resp = _upload(auth_client, _scanned_pdf(pages=3), "scan.pdf")
    body = resp.get_json()
    assert resp.status_code == 200, body
    assert sorted(n for n, _ in calls) == [1, 2]  # two pages per call
    assert all(0 < t <= 22 for _, t in calls)
    assert len(body["rows"]) == 3
    assert {r["currency"] for r in body["rows"]} == {"GBP"}
    assert body["totals"] == "match"
    assert "scan or a photo" in body["notice"]


def test_a_photo_of_a_statement(auth_client):
    with patch("artha.services.ai_service.AIService.extract_scanned_statement", return_value=PAGE_ONE) as read:
        resp = _upload(auth_client, _photo(), "IMG_2041.jpg")
    assert resp.status_code == 200
    (images,), _ = read.call_args
    media_type, data = images[0]
    assert media_type == "image/jpeg" and data  # shrunk and re-encoded
    assert len(resp.get_json()["rows"]) == 2


def test_a_photo_artha_cant_open_says_what_to_do(auth_client):
    resp = _upload(auth_client, b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64, "IMG_2041.HEIC")
    assert resp.status_code == 400
    assert "JPEG or PNG" in resp.get_json()["message"]


def test_a_scan_that_cant_be_read_explains_itself(auth_client):
    with patch("artha.services.ai_service.AIService.extract_scanned_statement", return_value={"error": "down"}):
        resp = _upload(auth_client, _scanned_pdf(pages=1), "scan.pdf")
    assert resp.status_code == 400
    assert "couldn't read that scan" in resp.get_json()["message"]


def test_a_scan_isnt_started_when_the_time_budget_is_spent(auth_client, monkeypatch):
    from artha.blueprints.finance import routes
    monkeypatch.setattr(routes, "_IMPORT_TIME_BUDGET_SECONDS", 0.0)
    with patch("artha.services.ai_service.AIService.extract_scanned_statement") as read:
        resp = _upload(auth_client, _photo("PNG"), "statement.png")
    assert not read.called
    assert resp.status_code == 400


def test_the_ai_gets_the_pages_as_images_and_must_answer_with_the_tool():
    from types import SimpleNamespace
    from artha.services.ai_service import AIService

    reply = SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={"transactions": [], "closing_balance": 10.0})])
    with patch("artha.services.ai_service._get_client") as get_client:
        get_client.return_value.messages.create.return_value = reply
        result = AIService.extract_scanned_statement([("image/jpeg", "AAAA")], timeout=12.5)
    kwargs = get_client.return_value.messages.create.call_args.kwargs
    content = kwargs["messages"][0]["content"]
    assert content[0] == {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}}
    assert kwargs["tool_choice"] == {"type": "tool", "name": "record_transactions"}
    assert kwargs["timeout"] == 12.5
    assert result["closing_balance"] == 10.0
