"""
Every endpoint that spends real Anthropic API money is rate limited per
account (extensions.ai_limit), so a retry loop, a stuck script, or a leaked
session can't run up an unbounded bill. The AI client is mocked to fail
fast here -- a request still counts against the limit whether or not the
model call itself succeeded, which is exactly the property that matters.
"""

from unittest.mock import patch

from flask import g

from .conftest import make_user

AJAX = {"X-Requested-With": "XMLHttpRequest"}


def _login(client, username):
    make_user(username=username, password="password123")
    client.post("/login", data={"username": username, "password": "password123"})


def test_ai_insights_is_limited_per_user_with_a_clear_message(rate_limited_client):
    _login(rate_limited_client, "dana")

    with patch("artha.services.ai_service._get_client", side_effect=RuntimeError("no key")):
        for _ in range(5):
            assert rate_limited_client.post("/api/ai/insights", headers=AJAX).status_code != 429
        blocked = rate_limited_client.post("/api/ai/insights", headers=AJAX)

    assert blocked.status_code == 429
    data = blocked.get_json()
    assert "AI limit" in data["message"]
    assert data["error"] == data["message"]  # ai.js reads `error`


def _post_as(client, url):
    # rate_limited_client keeps one app context open for the whole test, so
    # Flask-Login's per-request user cache (g._login_user) would otherwise
    # leak from one client's request into the other's. A real request
    # always starts with a fresh one.
    g.pop("_login_user", None)
    return client.post(url, headers=AJAX)


def test_one_users_ai_limit_doesnt_block_another_user_on_the_same_network(rate_limited_client):
    _login(rate_limited_client, "erin")
    other = rate_limited_client.application.test_client()
    _login(other, "frank")

    with patch("artha.services.ai_service._get_client", side_effect=RuntimeError("no key")):
        for _ in range(6):
            _post_as(rate_limited_client, "/api/ai/insights")
        assert _post_as(rate_limited_client, "/api/ai/insights").status_code == 429
        assert _post_as(other, "/api/ai/insights").status_code != 429


def test_calculator_ai_fallback_limit_answers_with_json_not_a_redirect(rate_limited_client):
    _login(rate_limited_client, "gina")

    with patch("artha.services.ai_service._get_client", side_effect=RuntimeError("no key")):
        responses = [
            rate_limited_client.post("/calculator/solve", json={"line": "how much is a dozen eggs"})
            for _ in range(31)
        ]

    last = responses[-1]
    assert last.status_code == 429
    assert "AI limit" in last.get_json()["message"]


def test_non_ai_routes_are_unaffected(rate_limited_client):
    _login(rate_limited_client, "hank")
    for _ in range(40):
        assert rate_limited_client.get("/finance").status_code == 200
