from datetime import datetime, timezone

from artha.extensions import db
from artha.models import Note

from .conftest import make_user


def _add_note(user, title, content, **extra):
    note = Note(title=title, content=content, user_id=user.id, **extra)
    db.session.add(note)
    db.session.commit()
    return note


def test_export_includes_active_and_archived_notes(auth_client, user):
    _add_note(user, "Grocery list", "Milk, eggs, bread")
    _add_note(user, "Old idea", "Something I put away", archived=True)

    resp = auth_client.get("/notes/export")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/csv")
    body = resp.get_data(as_text=True)
    assert "Grocery list" in body
    assert "Old idea" in body
    assert body.startswith("Title,Content,Tag,Pinned,Archived,Due Date,Created")


def test_export_excludes_trashed_notes(auth_client, user):
    _add_note(user, "Kept", "still around")
    _add_note(user, "Gone", "on its way out", deleted_at=datetime.now(timezone.utc))

    body = auth_client.get("/notes/export").get_data(as_text=True)
    assert "Kept" in body
    assert "Gone" not in body


def test_export_flattens_html_content_to_plain_text(auth_client, user):
    _add_note(user, "Formatted", "<p>Line one</p><p>Line two</p>")

    body = auth_client.get("/notes/export").get_data(as_text=True)
    assert "<p>" not in body
    assert "Line one" in body
    assert "Line two" in body


def test_export_neutralizes_formula_injection_in_title_and_content(auth_client, user):
    _add_note(user, "=cmd|'/c calc'!A1", "+1+1")

    body = auth_client.get("/notes/export").get_data(as_text=True)
    lines = [line for line in body.strip().split("\r\n") if line]
    data_line = lines[1]
    assert data_line.startswith("\"'=cmd") or data_line.startswith("'=cmd")
    assert "'+1+1" in data_line


def test_export_only_includes_current_users_notes(auth_client, user):
    other = make_user(username="mallory", password="password123")
    _add_note(user, "Mine", "content")
    _add_note(other, "Not mine", "content")

    body = auth_client.get("/notes/export").get_data(as_text=True)
    assert "Mine" in body
    assert "Not mine" not in body


def test_export_requires_login(client):
    resp = client.get("/notes/export", follow_redirects=False)
    assert resp.status_code in (302, 401)


def test_export_with_no_notes_returns_header_only(auth_client):
    body = auth_client.get("/notes/export").get_data(as_text=True)
    lines = [line for line in body.strip().split("\r\n") if line]
    assert lines == ["Title,Content,Tag,Pinned,Archived,Due Date,Created"]
