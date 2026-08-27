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
    notify_events_due = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())

    # One of currency.js's CURRENCY_PRESETS ("USD", "GBP", ...), or None
    # if this account has never explicitly saved one — a brand-new device
    # then has nothing to inherit and falls back to USD, same as today.
    preferred_currency = db.Column(db.String(3), nullable=True)

    # An IANA zone name (e.g. "America/Los_Angeles"), detected client-side
    # from the browser and silently kept in sync (static/js/settings.js).
    # None until the first authenticated page load reports one. Every
    # "today" the server computes for this user (the AI Assistant's system
    # prompt, "due today" reminders) should go through utils.user_today()/
    # user_now() rather than a bare UTC clock, or it silently drifts a day
    # for anyone not physically in UTC once the two dates roll over at
    # different real-world moments.
    timezone = db.Column(db.String(64), nullable=True)

    notes = db.relationship(
        "Note", backref="author", lazy="dynamic", cascade="all, delete-orphan"
    )
    transactions = db.relationship(
        "Transaction", backref="owner", lazy="dynamic", cascade="all, delete-orphan"
    )
    conversations = db.relationship(
        "Conversation", backref="user", lazy="dynamic", cascade="all, delete-orphan"
    )
    # Same "delete the user, delete their rows" contract as notes/transactions
    # above. These are ORM-only cascades (the FK constraints have no DB-level
    # ON DELETE CASCADE, matching every other relationship here) — without
    # them, deleting a User orphans these rows with a dangling user_id.
    events = db.relationship(
        "Event", backref="user", lazy="dynamic", cascade="all, delete-orphan"
    )
    push_subscriptions = db.relationship(
        "PushSubscription", backref="user", lazy="dynamic", cascade="all, delete-orphan"
    )
    scenarios = db.relationship(
        "Scenario", backref="user", lazy="dynamic", cascade="all, delete-orphan"
    )

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def __repr__(self) -> str:
        return f"<User {self.username}>"
