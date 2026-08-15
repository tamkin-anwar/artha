from artha.extensions import db
from artha.models.feedback import Feedback


def test_submit_feedback_returns_fresh_open_count(auth_client, user):
    db.session.add(Feedback(user_id=user.id, message="existing", status="new"))
    db.session.commit()

    resp = auth_client.post("/feedback", json={"category": "bug", "message": "Something broke"})
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["open_count"] == 2


def test_submit_feedback_requires_message(auth_client):
    resp = auth_client.post("/feedback", json={"category": "bug", "message": "  "})
    assert resp.status_code == 400
