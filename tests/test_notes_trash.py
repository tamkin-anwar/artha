from datetime import datetime, timedelta

from artha.blueprints.notes.routes import TRASH_RETENTION_DAYS, _purge_expired_trash
from artha.extensions import db
from artha.models import Note


def _make_note(user, title="Note", archived=False, deleted_at=None):
    note = Note(title=title, content="x", user_id=user.id, archived=archived, deleted_at=deleted_at)
    db.session.add(note)
    db.session.commit()
    return note


def test_patch_deleted_true_moves_note_to_trash(app, auth_client, user):
    note = _make_note(user, title="Old idea", archived=True)
    resp = auth_client.patch(f"/notes/{note.id}/update", json={"deleted": True})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["deleted_at"] is not None
    assert data["archived"] is True

    refreshed = db.session.get(Note, note.id)
    assert refreshed.deleted_at is not None


def test_trashing_a_note_forces_it_archived(app, auth_client, user):
    # Only reachable in the UI from the Archived view (already archived),
    # but the endpoint itself shouldn't rely on that — force it so a
    # restore always has exactly one destination.
    note = _make_note(user, title="Not yet archived", archived=False)
    resp = auth_client.patch(f"/notes/{note.id}/update", json={"deleted": True})
    assert resp.status_code == 200
    assert resp.get_json()["archived"] is True


def test_trashed_note_hidden_from_active_and_archived_views(app, auth_client, user):
    note = _make_note(user, title="Gone for now", archived=True)
    auth_client.patch(f"/notes/{note.id}/update", json={"deleted": True})

    active_body = auth_client.get("/notes").get_data(as_text=True)
    assert "Gone for now" not in active_body

    archived_body = auth_client.get("/notes?view=archived").get_data(as_text=True)
    assert "Gone for now" not in archived_body

    trash_body = auth_client.get("/notes?view=trash").get_data(as_text=True)
    assert "Gone for now" in trash_body


def test_restoring_from_trash_lands_in_archived_not_active(app, auth_client, user):
    note = _make_note(user, title="Back again", archived=True, deleted_at=datetime.utcnow())
    resp = auth_client.patch(f"/notes/{note.id}/update", json={"deleted": False})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["deleted_at"] is None
    assert data["archived"] is True

    archived_body = auth_client.get("/notes?view=archived").get_data(as_text=True)
    assert "Back again" in archived_body
    active_body = auth_client.get("/notes").get_data(as_text=True)
    assert "Back again" not in active_body


def test_delete_note_endpoint_is_real_permanent_deletion(app, auth_client, user):
    note = _make_note(user, title="Really gone", archived=True, deleted_at=datetime.utcnow())
    note_id = note.id
    resp = auth_client.post(f"/delete_note/{note_id}", headers={"X-Requested-With": "XMLHttpRequest"})
    assert resp.status_code == 200
    assert db.session.get(Note, note_id) is None


def test_trash_count_shown_in_active_and_archived_views(app, auth_client, user):
    _make_note(user, title="One", archived=True, deleted_at=datetime.utcnow())
    _make_note(user, title="Two", archived=True, deleted_at=datetime.utcnow())

    active_body = auth_client.get("/notes").get_data(as_text=True)
    assert "Trash (2)" in active_body

    archived_body = auth_client.get("/notes?view=archived").get_data(as_text=True)
    assert "Trash (2)" in archived_body


def test_trashed_notes_excluded_from_archived_count(app, auth_client, user):
    _make_note(user, title="Still archived", archived=True)
    _make_note(user, title="Trashed too", archived=True, deleted_at=datetime.utcnow())

    body = auth_client.get("/notes").get_data(as_text=True)
    assert "Archived (1)" in body


def test_trashed_note_tag_scoped_to_trash_view_only(app, auth_client, user):
    trashed = _make_note(user, title="Old", archived=True, deleted_at=datetime.utcnow())
    trashed.tag = "someday"
    db.session.commit()

    active_body = auth_client.get("/notes").get_data(as_text=True)
    assert 'class="notes-chip" data-tag="someday"' not in active_body

    archived_body = auth_client.get("/notes?view=archived").get_data(as_text=True)
    assert 'class="notes-chip" data-tag="someday"' not in archived_body

    trash_body = auth_client.get("/notes?view=trash").get_data(as_text=True)
    assert 'class="notes-chip" data-tag="someday"' in trash_body


def test_lazy_purge_removes_notes_past_retention_window(app, user):
    expired_at = datetime.utcnow() - timedelta(days=TRASH_RETENTION_DAYS + 1)
    fresh_at = datetime.utcnow() - timedelta(days=1)
    expired_id = _make_note(user, title="Too old", archived=True, deleted_at=expired_at).id
    fresh_id = _make_note(user, title="Still within window", archived=True, deleted_at=fresh_at).id

    _purge_expired_trash(user.id)

    assert db.session.get(Note, expired_id) is None
    assert db.session.get(Note, fresh_id) is not None


def test_lazy_purge_runs_on_notes_page_load(app, auth_client, user):
    expired_at = datetime.utcnow() - timedelta(days=TRASH_RETENTION_DAYS + 1)
    note = _make_note(user, title="Ancient trash", archived=True, deleted_at=expired_at)
    note_id = note.id

    auth_client.get("/notes")

    assert db.session.get(Note, note_id) is None


def test_lazy_purge_only_affects_requesting_users_notes(app, user):
    from tests.conftest import make_user

    other = make_user(username="bob-trash", password="password123")
    expired_at = datetime.utcnow() - timedelta(days=TRASH_RETENTION_DAYS + 1)
    mine_id = _make_note(user, title="Mine", archived=True, deleted_at=expired_at).id
    others_id = _make_note(other, title="Not mine", archived=True, deleted_at=expired_at).id

    _purge_expired_trash(user.id)

    assert db.session.get(Note, mine_id) is None
    assert db.session.get(Note, others_id) is not None


def test_stale_note_object_raises_on_commit_after_concurrent_purge(app, user):
    """Regression guard for the restore-vs-purge race: if a note is
    already loaded (as it would be mid-request in update_note_fields)
    when a concurrent purge deletes the same row on a separate connection
    (exactly what _purge_expired_trash / the cron command do), committing
    an attribute change on the now-stale object raises — this confirms
    what update_note_fields's restore path actually needs to catch to
    return a clean 410 instead of the generic 500 "Database error"."""
    import sqlalchemy as sa
    import pytest
    from sqlalchemy.orm.exc import StaleDataError

    note = _make_note(user, title="Racing restore", archived=True, deleted_at=datetime.utcnow() - timedelta(days=TRASH_RETENTION_DAYS + 1))
    note_id = note.id

    # Route-equivalent: the object is already loaded in this session...
    loaded = db.session.get(Note, note_id)
    assert loaded is not None

    # ...then a concurrent purge deletes the same row on a separate
    # connection, entirely outside this session — `loaded` stays exactly
    # as stale as it would mid-request in production.
    with db.engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM note WHERE id = :id"), {"id": note_id})

    loaded.deleted_at = None
    with pytest.raises(StaleDataError):
        db.session.commit()
    db.session.rollback()


def test_restore_returns_410_when_note_purged_concurrently(app, auth_client, user):
    """The actual route-level behavior confirmed by the mechanism test
    above: restoring a note that a concurrent purge deleted out from
    under the request returns a clean 410, not a false "Note updated" or
    a generic 500."""
    import sqlalchemy as sa

    note = _make_note(user, title="Racing restore", archived=True, deleted_at=datetime.utcnow() - timedelta(days=TRASH_RETENTION_DAYS + 1))
    note_id = note.id

    # Load it into this (shared, request-scoped) session first, same as
    # the route's own db.session.get() would, then delete the row on a
    # separate connection to simulate the concurrent purge without
    # expiring this session's already-loaded copy.
    db.session.get(Note, note_id)
    with db.engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM note WHERE id = :id"), {"id": note_id})

    resp = auth_client.patch(f"/notes/{note_id}/update", json={"deleted": False})
    assert resp.status_code == 410


def test_purge_expired_trash_cli_command_purges_across_all_users(app, user):
    from tests.conftest import make_user

    other = make_user(username="carol-trash", password="password123")
    expired_at = datetime.utcnow() - timedelta(days=TRASH_RETENTION_DAYS + 1)
    fresh_at = datetime.utcnow() - timedelta(days=1)
    expired_mine = _make_note(user, title="Old mine", archived=True, deleted_at=expired_at)
    expired_others = _make_note(other, title="Old theirs", archived=True, deleted_at=expired_at)
    fresh = _make_note(user, title="Fresh", archived=True, deleted_at=fresh_at)

    result = app.test_cli_runner().invoke(args=["purge-expired-trash"])

    assert result.exit_code == 0
    assert "Purged 2 note(s)" in result.output
    assert db.session.get(Note, expired_mine.id) is None
    assert db.session.get(Note, expired_others.id) is None
    assert db.session.get(Note, fresh.id) is not None
