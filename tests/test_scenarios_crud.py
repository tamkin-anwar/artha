from datetime import date, datetime, timezone
from decimal import Decimal

from artha.blueprints.scenarios.routes import _verdict
from artha.extensions import db
from artha.models import Transaction
from artha.models.scenario import Scenario

from .conftest import make_user


def _make_scenario(user, **kwargs):
    kwargs.setdefault("title", "Scenario")
    scenario = Scenario(user_id=user.id, **kwargs)
    db.session.add(scenario)
    db.session.commit()
    return scenario


def _add_tx(user, month: date, amount, tx_type):
    db.session.add(Transaction(
        description="x", amount=Decimal(amount), type=tx_type, user_id=user.id,
        timestamp=datetime(month.year, month.month, 15, 12, 0, tzinfo=timezone.utc),
    ))
    db.session.commit()


# --- Add -------------------------------------------------------------------

def test_add_scenario_creates_row_and_redirects_to_detail(auth_client, user):
    resp = auth_client.post("/scenarios/add", data={
        "title": "New apartment", "one_time_cost": "2000", "monthly_cost": "300",
        "monthly_savings": "0", "emotional_value": "7", "financial_risk": "4",
    }, follow_redirects=False)

    scenario = Scenario.query.filter_by(user_id=user.id, title="New apartment").first()
    assert scenario is not None
    assert resp.status_code == 302
    assert resp.headers["Location"] == f"/scenarios/{scenario.id}"
    assert scenario.one_time_cost == Decimal("2000")
    assert scenario.category == "other"  # default when omitted
    assert scenario.priority == "medium"  # default when omitted


def test_add_scenario_requires_title(auth_client, user):
    resp = auth_client.post("/scenarios/add", data={"title": ""}, follow_redirects=True)
    assert b"Title is required" in resp.data
    assert Scenario.query.filter_by(user_id=user.id).count() == 0


def test_add_scenario_rejects_non_numeric_cost(auth_client, user):
    resp = auth_client.post("/scenarios/add", data={"title": "X", "one_time_cost": "not-a-number"}, follow_redirects=True)
    assert b"must be a valid number" in resp.data
    assert Scenario.query.filter_by(user_id=user.id).count() == 0


def test_add_scenario_rejects_negative_cost(auth_client, user):
    resp = auth_client.post("/scenarios/add", data={"title": "X", "monthly_cost": "-50"}, follow_redirects=True)
    assert b"must be non-negative" in resp.data
    assert Scenario.query.filter_by(user_id=user.id).count() == 0


def test_add_scenario_rejects_end_date_before_start_date(auth_client, user):
    resp = auth_client.post("/scenarios/add", data={
        "title": "X", "start_date": "2026-06-01", "end_date": "2026-05-01",
    }, follow_redirects=True)
    # The flash message is rendered through Jinja's |tojson (inside a
    # showToast(...) script call), which escapes the apostrophe in "can't"
    # as ' rather than a literal quote — assert around it instead.
    body = resp.get_data(as_text=True)
    assert "End date can" in body
    assert "before start date" in body
    assert Scenario.query.filter_by(user_id=user.id).count() == 0


def test_add_scenario_falls_back_to_defaults_for_invalid_priority_and_status(auth_client, user):
    auth_client.post("/scenarios/add", data={"title": "X", "priority": "urgent", "status": "bogus"})
    scenario = Scenario.query.filter_by(user_id=user.id, title="X").first()
    assert scenario.priority == "medium"
    assert scenario.status == "active"


def test_add_scenario_page_requires_login(client):
    resp = client.get("/scenarios/add", follow_redirects=False)
    assert resp.status_code in (302, 401)


# --- Edit --------------------------------------------------------------------

def test_edit_scenario_updates_fields(auth_client, user):
    scenario = _make_scenario(user, title="Old title", monthly_cost=Decimal("100"))

    resp = auth_client.post(f"/scenarios/{scenario.id}/edit", data={
        "title": "New title", "monthly_cost": "250", "monthly_savings": "0",
        "one_time_cost": "0", "emotional_value": "5", "financial_risk": "5",
    }, follow_redirects=False)

    assert resp.status_code == 302
    updated = db.session.get(Scenario, scenario.id)
    assert updated.title == "New title"
    assert updated.monthly_cost == Decimal("250")


def test_edit_scenario_invalid_data_leaves_existing_values_untouched(auth_client, user):
    scenario = _make_scenario(user, title="Keep me", monthly_cost=Decimal("100"))

    auth_client.post(f"/scenarios/{scenario.id}/edit", data={"title": ""}, follow_redirects=True)

    unchanged = db.session.get(Scenario, scenario.id)
    assert unchanged.title == "Keep me"
    assert unchanged.monthly_cost == Decimal("100")


def test_edit_scenario_owned_by_another_user_is_not_found(auth_client, user):
    other = make_user(username="scenario-owner-2", password="password123")
    scenario = _make_scenario(other, title="Not yours")

    resp = auth_client.get(f"/scenarios/{scenario.id}/edit")
    assert resp.status_code == 404


def test_edit_scenario_page_requires_login(client, user):
    scenario = _make_scenario(user)
    resp = client.get(f"/scenarios/{scenario.id}/edit", follow_redirects=False)
    assert resp.status_code in (302, 401)


# --- Delete ------------------------------------------------------------------

def test_delete_scenario_removes_row(auth_client, user):
    scenario = _make_scenario(user, title="Doomed")
    resp = auth_client.post(f"/scenarios/{scenario.id}/delete", follow_redirects=False)

    assert resp.status_code == 302
    assert db.session.get(Scenario, scenario.id) is None


def test_delete_scenario_ajax_returns_json(auth_client, user):
    scenario = _make_scenario(user, title="Doomed")
    resp = auth_client.post(
        f"/scenarios/{scenario.id}/delete", headers={"X-Requested-With": "XMLHttpRequest"}
    )
    assert resp.status_code == 200
    assert resp.get_json()["message"]
    assert db.session.get(Scenario, scenario.id) is None


def test_delete_scenario_owned_by_another_user_is_not_deleted(auth_client, user):
    other = make_user(username="scenario-owner-3", password="password123")
    scenario = _make_scenario(other, title="Not yours")

    resp = auth_client.post(f"/scenarios/{scenario.id}/delete", follow_redirects=True)

    assert b"Scenario not found" in resp.data
    assert db.session.get(Scenario, scenario.id) is not None


def test_delete_scenario_requires_login(client, user):
    scenario = _make_scenario(user)
    resp = client.post(f"/scenarios/{scenario.id}/delete", follow_redirects=False)
    assert resp.status_code in (302, 401)
    assert db.session.get(Scenario, scenario.id) is not None


# --- Verdict math --------------------------------------------------------------

def test_verdict_do_it_when_still_comfortably_positive(app, user):
    month = date(2026, 3, 1)
    _add_tx(user, month, "5000", "income")
    _add_tx(user, month, "2000", "expense")
    scenario = _make_scenario(
        user, title="Cheap hobby", start_date=month, monthly_cost=Decimal("200"), financial_risk=3,
    )

    verdict = _verdict(scenario)
    assert verdict["label"] == "do_it"
    assert verdict["comparison"]["net"] == Decimal("3000")
    assert verdict["comparison"]["net_with_scenario"] == Decimal("2800")


def test_verdict_bad_idea_when_it_flips_a_positive_month_negative(app, user):
    month = date(2026, 4, 1)
    _add_tx(user, month, "1000", "income")
    _add_tx(user, month, "900", "expense")
    scenario = _make_scenario(
        user, title="Tips the balance", start_date=month, monthly_cost=Decimal("200"), financial_risk=4,
    )

    verdict = _verdict(scenario)
    assert verdict["label"] == "bad_idea"
    assert verdict["comparison"]["net"] == Decimal("100")
    assert verdict["comparison"]["net_with_scenario"] == Decimal("-100")
    assert "turning a positive month negative" in verdict["insight"]


def test_verdict_bad_idea_when_financial_risk_is_very_high_regardless_of_numbers(app, user):
    month = date(2026, 5, 1)
    _add_tx(user, month, "5000", "income")
    scenario = _make_scenario(user, title="Risky", start_date=month, financial_risk=8)

    verdict = _verdict(scenario)
    assert verdict["label"] == "bad_idea"


def test_verdict_wait_when_net_negative_but_risk_moderate(app, user):
    month = date(2026, 6, 1)
    _add_tx(user, month, "1000", "income")
    _add_tx(user, month, "1000", "expense")
    scenario = _make_scenario(
        user, title="Tight but not reckless", start_date=month, monthly_cost=Decimal("50"), financial_risk=4,
    )

    verdict = _verdict(scenario)
    assert verdict["label"] == "wait"


def test_verdict_risk_level_boundaries(app, user):
    month = date(2026, 7, 1)
    _add_tx(user, month, "5000", "income")

    low = _make_scenario(user, title="Low risk", start_date=month, financial_risk=3)
    medium = _make_scenario(user, title="Medium risk", start_date=month, financial_risk=6)
    high = _make_scenario(user, title="High risk", start_date=month, financial_risk=7)

    assert _verdict(low)["risk_level"] == "low"
    assert _verdict(medium)["risk_level"] == "medium"
    assert _verdict(high)["risk_level"] == "high"


def test_verdict_falls_back_to_projected_average_with_no_real_data_for_target_month(app, user):
    # A scenario dated for a month with zero transactions falls back to the
    # trailing 3-month average instead of a misleading $0/$0 real month.
    history_month = date.today().replace(day=1)
    _add_tx(user, history_month, "3000", "income")
    _add_tx(user, history_month, "1000", "expense")

    future = date(2030, 1, 1)
    scenario = _make_scenario(user, title="Future plan", start_date=future, financial_risk=5)

    verdict = _verdict(scenario)
    assert verdict["comparison"]["projected"] is True
    assert verdict["comparison"]["has_data"] is False
