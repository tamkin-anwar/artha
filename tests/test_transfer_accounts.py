"""Remembering the person's own accounts (models/transfer_account.py):
leave a line to "SAV 7837" out of one import, and the next import starts
lines to or from it unticked as transfers. Never learned from a card mask."""

import io

from artha.models import TransferAccount
from artha.services import statement_reader as sr


def _preview(client, csv):
    return client.post("/finance/import/preview", data={"statement": (io.BytesIO(csv.encode()), "s.csv")},
                       content_type="multipart/form-data").get_json()


def test_account_references_are_account_words_not_card_masks():
    assert sr.account_reference("Online Banking transfer to SAV 7837 Confirmation# 1") == ("sav 7837", "SAV 7837")
    assert sr.account_reference("TRANSFER FROM CHK XXXXXX1234") == ("chk 1234", "CHK 1234")
    assert sr.account_reference("NEFT TO A/C 004512345521") == ("a/c 5521", "A/C 5521")
    assert sr.account_reference("CHECKCARD 0907 GOOGLE XXXX1234") is None
    assert sr.account_reference("STARBUCKS STORE 51971") is None


def test_leaving_a_transfer_out_teaches_the_account(auth_client, user):
    csv = ("Date,Description,Amount\n"
           "2026-09-08,Mobile transfer to MY SAV 7837,-500.00\n"
           "2026-09-10,STARBUCKS,-5.95\n")
    rows = _preview(auth_client, csv)["rows"]
    sav = next(r for r in rows if r["account_ref"] == "SAV 7837")

    resp = auth_client.post("/finance/import/commit", json={
        "rows": [r for r in rows if r is not sav],
        "left_out": [sav],
    })
    assert resp.status_code == 200, resp.get_json()
    assert [a.label for a in TransferAccount.query.filter_by(user_id=user.id)] == ["SAV 7837"]

    later = _preview(auth_client, "Date,Description,Amount\n2026-10-08,Deposit from SAV XXXXX7837,250.00\n")["rows"]
    assert later[0]["transfer"] is True


def test_a_duplicate_or_a_line_without_an_account_teaches_nothing(auth_client, user):
    auth_client.post("/finance/import/commit", json={
        "rows": [{"date": "2026-09-10", "description": "Coffee", "amount": "5.00", "type": "expense"}],
        "left_out": [{"account_ref": "SAV 1111", "duplicate": True}, {"account_ref": None}, {"account_ref": "XXXX4444"}],
    })
    assert TransferAccount.query.filter_by(user_id=user.id).count() == 0


def test_the_app_lists_and_forgets_remembered_accounts(client, user):
    from artha.extensions import db
    db.session.add(TransferAccount(user_id=user.id, key="sav 7837", label="SAV 7837"))
    db.session.commit()
    token = client.post("/api/v1/sessions", json={"username": user.username, "password": "password123"}).get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    accounts = client.get("/api/v1/me/transfer-accounts", headers=headers).get_json()["accounts"]
    assert [a["label"] for a in accounts] == ["SAV 7837"]
    assert client.delete(f"/api/v1/me/transfer-accounts/{accounts[0]['id']}", headers=headers).status_code == 204
    assert TransferAccount.query.filter_by(user_id=user.id).count() == 0


def test_settings_shows_them_with_forget(auth_client, user):
    from artha.extensions import db
    db.session.add(TransferAccount(user_id=user.id, key="chk 1234", label="CHK 1234"))
    db.session.commit()
    page = auth_client.get("/settings").get_data(as_text=True)
    assert "Your accounts" in page and "CHK 1234" in page
    account = TransferAccount.query.filter_by(user_id=user.id).one()
    auth_client.post(f"/settings/transfer-accounts/{account.id}/forget")
    assert TransferAccount.query.filter_by(user_id=user.id).count() == 0


def test_deleting_the_user_deletes_their_accounts(app, user):
    from artha.extensions import db
    db.session.add(TransferAccount(user_id=user.id, key="sav 1", label="SAV 0001"))
    db.session.commit()
    db.session.delete(user)
    db.session.commit()
    assert TransferAccount.query.count() == 0
