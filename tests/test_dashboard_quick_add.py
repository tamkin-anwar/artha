from artha.extensions import db
from artha.models import Note

AJAX_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


def test_quick_add_note_ajax_returns_json(auth_client, user):
    resp = auth_client.post("/", data={"note": "Buy milk"}, headers=AJAX_HEADERS)
    assert resp.status_code == 200
    assert resp.get_json()["message"] == "Note added!"
    note = Note.query.filter_by(user_id=user.id).first()
    assert note is not None
    assert note.content == "Buy milk"


def test_quick_add_note_ajax_empty_content_returns_400(auth_client, user):
    resp = auth_client.post("/", data={"note": "   "}, headers=AJAX_HEADERS)
    assert resp.status_code == 400
    assert "message" in resp.get_json()
    assert Note.query.filter_by(user_id=user.id).count() == 0


def test_quick_add_note_non_ajax_still_redirects(auth_client, user):
    resp = auth_client.post("/", data={"note": "Buy milk"})
    assert resp.status_code == 302
    assert Note.query.filter_by(user_id=user.id).count() == 1
