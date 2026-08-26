from datetime import datetime, timedelta, timezone
from decimal import Decimal

from artha.extensions import db
from artha.models import Event, Note, Transaction
from artha.models.scenario import Scenario

from .conftest import make_user


def test_search_requires_login(client):
    resp = client.get("/search?q=abc", follow_redirects=False)
    assert resp.status_code in (302, 401)


def test_search_short_query_returns_empty(auth_client):
    resp = auth_client.get("/search?q=a")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {"notes": [], "transactions": [], "scenarios": [], "events": []}


def test_search_finds_note_by_title_and_content(auth_client, user):
    db.session.add(Note(title="Grocery list", content="milk eggs bread", user_id=user.id))
    db.session.add(Note(title="Unrelated", content="something else entirely", user_id=user.id))
    db.session.commit()

    data = auth_client.get("/search?q=grocery").get_json()
    assert len(data["notes"]) == 1
    assert data["notes"][0]["title"] == "Grocery list"

    data2 = auth_client.get("/search?q=eggs").get_json()
    assert len(data2["notes"]) == 1
    assert data2["notes"][0]["title"] == "Grocery list"


def test_search_excludes_archived_and_trashed_notes(auth_client, user):
    db.session.add(Note(title="Findme archived", content="x", user_id=user.id, archived=True))
    db.session.add(Note(title="Findme trashed", content="x", user_id=user.id, deleted_at=datetime.utcnow()))
    db.session.add(Note(title="Findme active", content="x", user_id=user.id))
    db.session.commit()

    data = auth_client.get("/search?q=findme").get_json()
    titles = [n["title"] for n in data["notes"]]
    assert titles == ["Findme active"]


def test_search_finds_transaction_by_description(auth_client, user):
    db.session.add(Transaction(description="Coffee shop", amount=Decimal("4.50"), type="expense", user_id=user.id, timestamp=datetime.now(timezone.utc)))
    db.session.commit()

    data = auth_client.get("/search?q=coffee").get_json()
    assert len(data["transactions"]) == 1
    assert data["transactions"][0]["title"] == "Coffee shop"
    assert "4.50" in data["transactions"][0]["snippet"]


def test_search_finds_transaction_by_category(auth_client, user):
    # "Trader Joe's" never contains the word "groceries" — only the
    # category field does, so this only passes once category is searched.
    db.session.add(Transaction(
        description="Trader Joe's", amount=Decimal("62.10"), type="expense",
        category="groceries", user_id=user.id, timestamp=datetime.now(timezone.utc),
    ))
    db.session.commit()

    data = auth_client.get("/search?q=groceries").get_json()
    assert len(data["transactions"]) == 1
    assert data["transactions"][0]["title"] == "Trader Joe's"


def test_search_finds_scenario_by_title_excludes_archived(auth_client, user):
    db.session.add(Scenario(title="Move to Boston", user_id=user.id, status="active"))
    db.session.add(Scenario(title="Move to Seattle", user_id=user.id, status="archived"))
    db.session.commit()

    data = auth_client.get("/search?q=move").get_json()
    titles = [s["title"] for s in data["scenarios"]]
    assert titles == ["Move to Boston"]


def test_search_finds_scenario_by_description_and_notes(auth_client, user):
    db.session.add(Scenario(title="Untitled plan", description="A 4-day work week", user_id=user.id, status="active"))
    db.session.add(Scenario(title="Another plan", notes="thinking about a sabbatical", user_id=user.id, status="active"))
    db.session.commit()

    by_desc = auth_client.get("/search?q=work+week").get_json()
    assert [s["title"] for s in by_desc["scenarios"]] == ["Untitled plan"]

    by_notes = auth_client.get("/search?q=sabbatical").get_json()
    assert [s["title"] for s in by_notes["scenarios"]] == ["Another plan"]


def test_search_finds_event_by_title(auth_client, user):
    start = datetime.now(timezone.utc) + timedelta(days=1)
    db.session.add(Event(title="Dentist appointment", user_id=user.id, start=start, end=start + timedelta(hours=1)))
    db.session.commit()

    data = auth_client.get("/search?q=dentist").get_json()
    assert len(data["events"]) == 1
    assert data["events"][0]["title"] == "Dentist appointment"


def test_search_scoped_to_current_user_only(auth_client, user):
    other = make_user(username="bob-search", password="password123")
    db.session.add(Note(title="Mine unique term", content="x", user_id=user.id))
    db.session.add(Note(title="Not mine unique term", content="x", user_id=other.id))
    db.session.commit()

    data = auth_client.get("/search?q=unique").get_json()
    titles = [n["title"] for n in data["notes"]]
    assert titles == ["Mine unique term"]


def test_search_results_capped_per_category(auth_client, user):
    for i in range(11):
        db.session.add(Note(title=f"Capped note {i}", content="x", user_id=user.id))
    db.session.commit()

    data = auth_client.get("/search?q=capped").get_json()
    assert len(data["notes"]) == 8
