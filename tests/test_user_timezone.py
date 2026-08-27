from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from artha.utils import user_now, user_today


def _fake_user(tz):
    return SimpleNamespace(timezone=tz)


def test_user_now_converts_to_the_stored_timezone():
    # 3am UTC on Jan 1 is still 7pm on Dec 31 in Los Angeles (UTC-8 in
    # January) — a real, deterministic case where "today" genuinely
    # differs depending on whose clock you ask.
    fixed_utc = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)
    with patch("artha.utils.datetime") as mock_dt:
        mock_dt.now.side_effect = lambda tz=None: (
            fixed_utc.astimezone(tz) if tz is not None else fixed_utc
        )
        result = user_now(_fake_user("America/Los_Angeles"))

    assert result.date() == date(2025, 12, 31)
    assert result.utcoffset() is not None and result.utcoffset().total_seconds() == -8 * 3600


def test_user_today_matches_user_now_date():
    fixed_utc = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)
    with patch("artha.utils.datetime") as mock_dt:
        mock_dt.now.side_effect = lambda tz=None: (
            fixed_utc.astimezone(tz) if tz is not None else fixed_utc
        )
        assert user_today(_fake_user("America/Los_Angeles")) == date(2025, 12, 31)


def test_user_now_falls_back_to_utc_when_timezone_not_set():
    result = user_now(_fake_user(None))
    assert result.tzinfo is timezone.utc


def test_user_now_falls_back_to_utc_for_an_invalid_timezone_name():
    result = user_now(_fake_user("Not/AZone"))
    assert result.tzinfo is timezone.utc


def test_set_timezone_requires_login(client):
    resp = client.post("/set_timezone", json={"timezone": "America/Los_Angeles"}, follow_redirects=False)
    assert resp.status_code in (302, 401)


def test_set_timezone_updates_account(auth_client, user):
    resp = auth_client.post("/set_timezone", json={"timezone": "America/Los_Angeles"})
    assert resp.status_code == 200
    assert user.timezone == "America/Los_Angeles"


def test_set_timezone_rejects_unknown_zone(auth_client, user):
    resp = auth_client.post("/set_timezone", json={"timezone": "Not/AZone"})
    assert resp.status_code == 400
    assert user.timezone is None
