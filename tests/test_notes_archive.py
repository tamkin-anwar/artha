from artha.extensions import db
from artha.models import Note


def _make_note(user, title="Note", archived=False, pinned=False):
    note = Note(title=title, content="x", user_id=user.id, archived=archived, pinned=pinned)
    db.session.add(note)
    db.session.commit()
    return note


def test_new_note_defaults_to_not_archived(app, auth_client):
    resp = auth_client.post("/notes/new")
    assert resp.status_code == 200
    assert resp.get_json()["archived"] is False


def test_patch_archived_true_removes_note_from_default_view(app, auth_client, user):
    note = _make_note(user, title="Old idea")
    resp = auth_client.patch(f"/notes/{note.id}/update", json={"archived": True})
    assert resp.status_code == 200
    assert resp.get_json()["archived"] is True

    body = auth_client.get("/notes").get_data(as_text=True)
    assert "Old idea" not in body


def test_archived_view_shows_only_archived_notes(app, auth_client, user):
    _make_note(user, title="Active note", archived=False)
    _make_note(user, title="Archived note", archived=True)

    default_body = auth_client.get("/notes").get_data(as_text=True)
    assert "Active note" in default_body
    assert "Archived note" not in default_body

    archived_body = auth_client.get("/notes?view=archived").get_data(as_text=True)
    assert "Archived note" in archived_body
    assert "Active note" not in archived_body


def test_unarchiving_restores_note_to_default_view(app, auth_client, user):
    note = _make_note(user, title="Coming back", archived=True)
    resp = auth_client.patch(f"/notes/{note.id}/update", json={"archived": False})
    assert resp.status_code == 200
    assert resp.get_json()["archived"] is False

    body = auth_client.get("/notes").get_data(as_text=True)
    assert "Coming back" in body


def test_delete_note_from_archived_view_is_now_permanent(app, auth_client, user):
    # /delete_note is real, unconditional deletion now — moving a note to
    # Trash (the reversible step) is a PATCH {deleted: true} via
    # update_note_fields, exercised in test_notes_trash.py. This endpoint
    # is only ever reached from the Trash view's "Delete forever" button.
    note = _make_note(user, title="Filed away", archived=True)
    note_id = note.id
    resp = auth_client.post(f"/delete_note/{note_id}", headers={"X-Requested-With": "XMLHttpRequest"})
    assert resp.status_code == 200

    assert db.session.get(Note, note_id) is None


def test_archived_notes_dont_appear_in_active_filter_chip_tags(app, auth_client, user):
    archived = _make_note(user, title="Old", archived=True)
    archived.tag = "someday"
    db.session.commit()

    body = auth_client.get("/notes").get_data(as_text=True)
    assert 'class="notes-chip" data-tag="someday"' not in body

    archived_body = auth_client.get("/notes?view=archived").get_data(as_text=True)
    assert 'class="notes-chip" data-tag="someday"' in archived_body


def test_archived_count_shown_in_default_view(app, auth_client, user):
    _make_note(user, title="One", archived=True)
    _make_note(user, title="Two", archived=True)

    body = auth_client.get("/notes").get_data(as_text=True)
    assert "Archived (2)" in body
