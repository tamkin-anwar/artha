from datetime import date
from unittest.mock import patch

from artha.extensions import db
from artha.models import Note


def test_index_uses_the_users_local_today_not_utc(auth_client, user):
    # A fixed, arbitrary date the test fully controls — decouples this
    # from real wall-clock/timezone math (already covered by
    # tests/test_user_timezone.py) and just proves the route actually
    # calls user_today() and that its result reaches the page.
    fixed_today = date(2026, 3, 15)
    note = Note(title="Renew passport", content="x", user_id=user.id, due_date=fixed_today)
    db.session.add(note)
    db.session.commit()

    with patch("artha.blueprints.dashboard.routes.user_today", return_value=fixed_today):
        resp = auth_client.get("/")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Renew passport" in html
    assert "Due today" in html
    assert "Overdue" not in html


def test_calendar_page_uses_the_users_local_today_not_utc(auth_client):
    fixed_today = date(2026, 3, 15)

    with patch("artha.blueprints.dashboard.routes.user_today", return_value=fixed_today):
        resp = auth_client.get("/calendar")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'window.CALENDAR_TODAY = "2026-03-15"' in html
