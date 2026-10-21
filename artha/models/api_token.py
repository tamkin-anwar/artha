import hashlib
import secrets
from datetime import datetime, timezone

from ..extensions import db


class ApiToken(db.Model):
    """A sign-in for the native Artha app (the artha-apple repo), one row
    per device. The app sends the raw token as `Authorization: Bearer ...`
    on every /api/v1 request (blueprints/api) instead of a session cookie,
    which is what lets the API skip CSRF entirely: nothing a browser would
    attach on its own ever authenticates an API call.

    Only a SHA-256 hash of the token is stored, never the token itself --
    the same reason password_hash isn't a password: a leaked database
    can't be replayed against the API. Plain SHA-256 rather than a slow
    password hash is deliberate and standard for this shape of secret:
    the raw value is 256 bits from secrets.token_urlsafe, not something a
    person chose, so there's nothing to brute-force, and every API request
    has to look it up.

    No expiry: a phone stays signed in until the person signs out, changes
    or resets their password, or deletes their account, all of which
    delete the row (see auth/routes.py and api/routes.py)."""

    __tablename__ = "api_token"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    # Whatever the app reports for itself ("Tamkin's iPhone"), so a future
    # "signed-in devices" list has something human to show. Optional.
    device_name = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    # Updated at most every few minutes (see api/routes.py), not on every
    # request, so reading data doesn't turn into a write per call.
    last_used_at = db.Column(db.DateTime, nullable=True)

    @staticmethod
    def hash_token(raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def issue(cls, user, device_name: str | None = None) -> tuple["ApiToken", str]:
        """Creates (but doesn't commit) a token for `user` and returns it
        alongside the raw value -- the only moment the raw value exists
        anywhere on the server."""
        raw = secrets.token_urlsafe(32)
        name = (device_name or "").strip()[:100] or None
        token = cls(user_id=user.id, token_hash=cls.hash_token(raw), device_name=name)
        db.session.add(token)
        return token, raw

    @classmethod
    def revoke_all_for(cls, user_id: int) -> None:
        """Signs every device out. A bulk Query.delete is safe here (unlike
        for User itself) because ApiToken has no children to cascade.
        Caller commits."""
        cls.query.filter_by(user_id=user_id).delete(synchronize_session=False)

    def __repr__(self) -> str:
        return f"<ApiToken {self.id} user={self.user_id}>"
