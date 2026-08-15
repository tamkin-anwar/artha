from datetime import date

from artha.extensions import db
from artha.models import Note


def _make_note(user, title="Note", archived=False, due_date=None, position=0):
    note = Note(
        title=title,
        content="x",
        user_id=user.id,
        archived=archived,
        due_date=due_date,
        position=position,
    )
    db.session.add(note)
    db.session.commit()
    return note


def test_dashboard_recent_notes_widget_excludes_archived(app, auth_client, user):
    _make_note(user, title="Active note", archived=False)
    _make_note(user, title="Archived note", archived=True)

    body = auth_client.get("/").get_data(as_text=True)
    assert "Active note" in body
    assert "Archived note" not in body


def test_dashboard_overdue_and_due_today_exclude_archived(app, auth_client, user):
    today = date.today()
    _make_note(user, title="Active overdue", archived=False, due_date=today)
    _make_note(user, title="Archived overdue", archived=True, due_date=today)

    body = auth_client.get("/").get_data(as_text=True)
    assert "Active overdue" in body
    assert "Archived overdue" not in body


def test_calendar_due_markers_exclude_archived(app, auth_client, user):
    today = date.today()
    _make_note(user, title="Active calendar note", archived=False, due_date=today)
    _make_note(user, title="Archived calendar note", archived=True, due_date=today)

    body = auth_client.get("/calendar").get_data(as_text=True)
    assert "Active calendar note" in body
    assert "Archived calendar note" not in body
