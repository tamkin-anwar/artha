from artha.extensions import db
from artha.models.scenario import Scenario


def _make_scenario(user, title="Scenario", status="active"):
    scenario = Scenario(user_id=user.id, title=title, status=status)
    db.session.add(scenario)
    db.session.commit()
    return scenario


def test_index_excludes_archived_by_default(auth_client, user):
    _make_scenario(user, title="Active scenario", status="active")
    _make_scenario(user, title="Archived scenario", status="archived")

    body = auth_client.get("/scenarios/").get_data(as_text=True)
    assert "Active scenario" in body
    assert "Archived scenario" not in body


def test_index_status_filter_shows_only_archived(auth_client, user):
    _make_scenario(user, title="Active scenario", status="active")
    archived = _make_scenario(user, title="Archived scenario", status="archived")

    body = auth_client.get("/scenarios/?status=archived").get_data(as_text=True)
    assert "Archived scenario" in body
    assert "Active scenario" not in body
    assert f"/scenarios/{archived.id}" in body


def test_detail_sidebar_excludes_other_archived_scenarios(auth_client, user):
    active = _make_scenario(user, title="Active scenario", status="active")
    _make_scenario(user, title="Archived scenario", status="archived")

    body = auth_client.get(f"/scenarios/{active.id}").get_data(as_text=True)
    assert "Active scenario" in body
    assert "Archived scenario" not in body


def test_detail_of_archived_scenario_still_renders_and_shows_itself(auth_client, user):
    archived = _make_scenario(user, title="Archived scenario", status="archived")

    body = auth_client.get(f"/scenarios/{archived.id}").get_data(as_text=True)
    assert "Archived scenario" in body
    assert "Back to scenarios" in body
