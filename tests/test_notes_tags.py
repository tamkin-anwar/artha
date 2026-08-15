from artha.blueprints.notes.routes import SEED_NOTE_TAGS, _normalize_tag, _tag_suggestions, _user_tags
from artha.extensions import db
from artha.models import Note


def _make_note(user, tag=None, content="x"):
    note = Note(content=content, user_id=user.id, tag=tag)
    db.session.add(note)
    db.session.commit()
    return note


def test_normalize_tag_trims_and_lowercases():
    assert _normalize_tag("  Project  ") == "project"


def test_normalize_tag_collapses_internal_whitespace():
    assert _normalize_tag("side   project") == "side project"


def test_normalize_tag_blank_becomes_none():
    assert _normalize_tag("") is None
    assert _normalize_tag("   ") is None
    assert _normalize_tag(None) is None


def test_normalize_tag_caps_length_at_column_limit():
    long_tag = "x" * 50
    assert len(_normalize_tag(long_tag)) == 30


def test_new_note_accepts_any_free_text_tag(app, auth_client, user):
    note = _make_note(user)
    resp = auth_client.patch(f"/notes/{note.id}/update", json={"tag": "Doorsong"})
    assert resp.status_code == 200
    refreshed = db.session.get(Note, note.id)
    assert refreshed.tag == "doorsong"


def test_setting_tag_with_different_casing_reuses_same_normalized_value(app, auth_client, user):
    note = _make_note(user)
    auth_client.patch(f"/notes/{note.id}/update", json={"tag": "Doorsong"})
    note2 = _make_note(user)
    auth_client.patch(f"/notes/{note2.id}/update", json={"tag": "doorsong "})

    tags = _user_tags(user.id)
    assert tags.count("doorsong") == 1


def test_clearing_tag_with_null(app, auth_client, user):
    note = _make_note(user, tag="project")
    resp = auth_client.patch(f"/notes/{note.id}/update", json={"tag": None})
    assert resp.status_code == 200
    assert db.session.get(Note, note.id).tag is None


def test_user_tags_only_returns_tags_actually_in_use(app, user):
    _make_note(user, tag="doorsong")
    _make_note(user, tag="doorsong")
    _make_note(user, tag=None)

    assert _user_tags(user.id) == ["doorsong"]


def test_user_tags_scoped_per_user(app, user):
    from tests.conftest import make_user

    other = make_user(username="mallory-tags", password="password123")
    _make_note(user, tag="project")
    _make_note(other, tag="doorsong")

    assert _user_tags(user.id) == ["project"]
    assert _user_tags(other.id) == ["doorsong"]


def test_tag_suggestions_include_seed_tags_even_with_no_notes(app, user):
    assert set(_tag_suggestions(user.id)) == SEED_NOTE_TAGS


def test_tag_suggestions_union_used_and_seed_tags(app, user):
    _make_note(user, tag="doorsong")
    suggestions = set(_tag_suggestions(user.id))
    assert suggestions == SEED_NOTE_TAGS | {"doorsong"}


def test_notes_page_filter_chips_reflect_real_usage_not_seed_tags(auth_client, user):
    _make_note(user, tag="doorsong")
    resp = auth_client.get("/notes")
    body = resp.get_data(as_text=True)
    assert 'class="notes-chip" data-tag="doorsong"' in body
    # Seed tags shouldn't appear as filter chips unless actually used — a
    # dead filter pill (matching zero notes) is worse than none. The tag
    # *picker* in the edit modal is a separate element (.note-tag-opt)
    # that's expected to still offer "personal" as a seed suggestion, so
    # this checks the filter-chip class specifically, not the whole page.
    assert 'class="notes-chip" data-tag="personal"' not in body
