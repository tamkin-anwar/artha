from datetime import datetime, timezone
from decimal import Decimal

from artha.extensions import db
from artha.models import Transaction

from .conftest import make_user


def _add_tx(user, description, amount, ttype, when=None):
    tx = Transaction(
        description=description,
        amount=Decimal(amount),
        type=ttype,
        user_id=user.id,
        timestamp=when or datetime.now(timezone.utc),
    )
    db.session.add(tx)
    db.session.commit()
    return tx


def test_export_current_month_by_default(auth_client, user):
    _add_tx(user, "Coffee", "4.50", "expense")
    _add_tx(user, "Paycheck", "2000.00", "income")

    resp = auth_client.get("/finance/export")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/csv")
    body = resp.get_data(as_text=True)
    assert "Coffee" in body
    assert "Paycheck" in body
    assert body.startswith("Date,Description,Type,Amount,Recurring")


def test_export_only_includes_current_users_transactions(auth_client, user):
    other = make_user(username="mallory", password="password123")
    _add_tx(user, "Mine", "10.00", "expense")
    _add_tx(other, "Not mine", "20.00", "expense")

    body = auth_client.get("/finance/export?month=all").get_data(as_text=True)
    assert "Mine" in body
    assert "Not mine" not in body


def test_export_requires_login(client):
    resp = client.get("/finance/export", follow_redirects=False)
    assert resp.status_code in (302, 401)


def test_export_empty_month_returns_header_only(auth_client):
    body = auth_client.get("/finance/export?month=1999-01").get_data(as_text=True)
    lines = [line for line in body.strip().split("\r\n") if line]
    assert lines == ["Date,Description,Type,Amount,Recurring"]
