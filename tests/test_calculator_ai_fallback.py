"""AI fallback for one Smart Calculator line (templates/calculator.html),
covering AIService.solve_calculator_line directly and the
/calculator/solve route that fronts it. The deterministic pipeline
itself is client-side JS with no pytest coverage — this file only
exercises the last-resort tier, same split as PDF-import's regex parser
(tested via real PDFs) vs. its AI fallback (tested via mocks) in
test_pdf_import.py.
"""

from types import SimpleNamespace
from unittest.mock import patch

from artha.services.ai_service import AIService


def _fake_solve_response(solvable, value=None, formula=None):
    """Mirrors the Anthropic SDK response shape for a forced tool_choice
    call — see AIService.solve_calculator_line, which reads exactly this
    shape back out via resp.content[i].type == "tool_use"."""
    return SimpleNamespace(
        content=[SimpleNamespace(
            type="tool_use",
            name="solve_line",
            input={"solvable": solvable, "value": value, "formula": formula},
        )],
        usage=SimpleNamespace(input_tokens=60, output_tokens=20),
    )


# ---------------------------------------------------------------------------
# AIService.solve_calculator_line
# ---------------------------------------------------------------------------

def test_solve_calculator_line_returns_a_solvable_result():
    with patch("artha.services.ai_service._get_client") as mock_get_client:
        mock_get_client.return_value.messages.create.return_value = _fake_solve_response(
            True, value=23.6, formula="60 * 1.18 / 3"
        )
        result = AIService.solve_calculator_line(
            "split a $60 bill 3 ways with 18% tip"
        )

    assert result == {"solvable": True, "value": 23.6, "formula": "60 * 1.18 / 3"}


def test_solve_calculator_line_reports_unsolvable_for_a_non_question():
    """The exact reported bug's shape: a sentence with numbers in it
    that isn't asking anything computable."""
    with patch("artha.services.ai_service._get_client") as mock_get_client:
        mock_get_client.return_value.messages.create.return_value = _fake_solve_response(False)
        result = AIService.solve_calculator_line(
            "41 men in wheelchairs and they need 300 cheeseburgers"
        )

    assert result == {"solvable": False}


def test_solve_calculator_line_rejects_a_non_finite_value():
    """A malformed or non-finite value can't be trusted any more than a
    wrong one can - same stance as categorize_transactions' length check."""
    with patch("artha.services.ai_service._get_client") as mock_get_client:
        mock_get_client.return_value.messages.create.return_value = _fake_solve_response(
            True, value=float("nan"), formula="0/0"
        )
        result = AIService.solve_calculator_line("something odd")

    assert result == {"solvable": False}


def test_solve_calculator_line_blank_line_short_circuits_without_a_call():
    with patch("artha.services.ai_service._get_client") as mock_get_client:
        result = AIService.solve_calculator_line("   ")
        mock_get_client.assert_not_called()

    assert result == {"solvable": False}


def test_solve_calculator_line_degrades_without_an_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # _get_client() caches its client in a module-level singleton once
    # initialized — force a fresh (and here, failing) init regardless of
    # whatever state an earlier test in the same process left behind.
    monkeypatch.setattr("artha.services.ai_service._client", None)

    result = AIService.solve_calculator_line("2 apples plus 3 apples")

    assert "error" in result


# ---------------------------------------------------------------------------
# POST /calculator/solve
# ---------------------------------------------------------------------------

def test_calculator_solve_route_requires_login(client):
    resp = client.post("/calculator/solve", json={"line": "1 + 1"})
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_calculator_solve_route_rejects_an_empty_line(auth_client):
    resp = auth_client.post("/calculator/solve", json={"line": "   "})
    assert resp.status_code == 400


def test_calculator_solve_route_rejects_an_overlong_line(auth_client):
    resp = auth_client.post("/calculator/solve", json={"line": "1 " * 200})
    assert resp.status_code == 400


def test_calculator_solve_route_returns_the_service_result(auth_client):
    with patch("artha.services.ai_service._get_client") as mock_get_client:
        mock_get_client.return_value.messages.create.return_value = _fake_solve_response(
            True, value=96.0, formula="80 * 0.85 * 0.85 * 1.66"
        )
        resp = auth_client.post(
            "/calculator/solve", json={"line": "how much is 15% off $80 twice"}
        )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["solvable"] is True
    assert data["value"] == 96.0


def test_calculator_solve_route_degrades_without_an_api_key(auth_client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("artha.services.ai_service._client", None)

    resp = auth_client.post("/calculator/solve", json={"line": "2 apples plus 3 apples"})

    assert resp.status_code == 503
    assert resp.get_json()["solvable"] is False
