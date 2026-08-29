"""/whats-new: a short, plain-language changelog for users, and the
top-bar badge that points to it. See artha/changelog.py for the content
and blueprints/dashboard/routes.py's inject_changelog_badge/whats_new.
"""

import re
from datetime import datetime, timedelta, timezone

from artha.changelog import CHANGELOG_ENTRIES


def _strip_entities(text):
    """Removes HTML entities (Jinja autoescapes ' and & differently
    depending on context) and the literal characters they replace, so a
    raw title string can be compared against rendered page text without
    having to match MarkupSafe's exact escaped form."""
    text = re.sub(r"&#?\w+;", "", text)
    return text.replace("'", "").replace("&", "")
from artha.extensions import db
from artha.models import User


def test_whats_new_requires_login(client):
    resp = client.get("/whats-new")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_whats_new_renders_every_entry(auth_client):
    resp = auth_client.get("/whats-new")
    assert resp.status_code == 200
    text = _strip_entities(resp.data.decode())
    for entry in CHANGELOG_ENTRIES:
        assert _strip_entities(entry["title"]) in text


def test_visiting_whats_new_marks_it_seen(auth_client, user):
    assert user.changelog_seen_at is None
    auth_client.get("/whats-new")
    refreshed = User.query.filter_by(username=user.username).first()
    assert refreshed.changelog_seen_at is not None


def test_badge_shows_for_a_user_who_has_never_seen_it(auth_client):
    resp = auth_client.get("/")
    assert resp.status_code == 200
    assert b'title="What\'s new"' in resp.data


def test_badge_clears_once_the_latest_entry_has_been_seen(auth_client, user):
    user.changelog_seen_at = datetime.now(timezone.utc)
    db.session.commit()

    resp = auth_client.get("/")

    assert resp.status_code == 200
    assert b"background:var(--gold); box-shadow" not in resp.data


def test_badge_reappears_for_a_stale_seen_timestamp(auth_client, user):
    """A user who saw the changelog a year ago, before today's entries
    existed, should still see the badge for what's new since then."""
    user.changelog_seen_at = datetime.now(timezone.utc) - timedelta(days=365)
    db.session.commit()

    resp = auth_client.get("/")

    assert resp.status_code == 200
    assert b"background:var(--gold); box-shadow" in resp.data
