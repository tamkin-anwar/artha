from artha.extensions import db
from artha.models.feedback import Feedback
from tests.conftest import make_user


def _make_admin(username="admin1"):
    return make_user(username=username, is_admin=True)


def _make_feedback(user, status="new", message="x"):
    item = Feedback(user_id=user.id, message=message, status=status)
    db.session.add(item)
    db.session.commit()
    return item


def test_update_status_returns_fresh_open_count(app, client, user):
    admin = _make_admin("admin2")
    _make_feedback(user, status="new")
    other = _make_feedback(user, status="new")
    client.post("/login", data={"username": admin.username, "password": "password123"})

    resp = client.patch(f"/admin/feedback/{other.id}/status", json={"status": "resolved"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "resolved"
    assert data["open_count"] == 1


def test_reopening_feedback_increments_open_count(app, client, user):
    admin = _make_admin("admin3")
    item = _make_feedback(user, status="resolved")
    client.post("/login", data={"username": admin.username, "password": "password123"})

    resp = client.patch(f"/admin/feedback/{item.id}/status", json={"status": "new"})
    assert resp.status_code == 200
    assert resp.get_json()["open_count"] == 1


def test_admin_sidebar_badge_reflects_open_count(app, client, user):
    admin = _make_admin("admin4")
    _make_feedback(user, status="new")
    _make_feedback(user, status="new")
    client.post("/login", data={"username": admin.username, "password": "password123"})

    body = client.get("/admin/").get_data(as_text=True)
    assert 'id="admin-badge-desktop"' in body
    assert "display:inline-flex" in body
