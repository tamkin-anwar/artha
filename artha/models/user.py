from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from ..extensions import db


class User(UserMixin, db.Model):
    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=True)
    first_name = db.Column(db.String(80), nullable=True)
    last_name = db.Column(db.String(80), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    # Never set from any user-facing form — only ever flipped directly in
    # the database for a trusted account. Gates access to the /admin blueprint.
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_login_at = db.Column(db.DateTime, nullable=True)

    # Which "due today" push reminders (flask send-renewal-reminders,
    # artha/cli.py) this user wants. Both default True so every existing
    # subscriber keeps getting exactly what they get today until they
    # actively turn one off.
    notify_bills_due = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    notify_notes_due = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())

    notes = db.relationship(
        "Note", backref="author", lazy="dynamic", cascade="all, delete-orphan"
    )
    transactions = db.relationship(
        "Transaction", backref="owner", lazy="dynamic", cascade="all, delete-orphan"
    )

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def __repr__(self) -> str:
        return f"<User {self.username}>"
