from datetime import datetime, timezone

from ..extensions import db


class RecoveryCode(db.Model):
    """
    A single-use two-factor recovery code — the fallback when a user can't
    produce a current TOTP code (lost phone, uninstalled authenticator).
    Ten are generated the moment 2FA is enabled (blueprints/auth/routes.py,
    /account/2fa/enable), shown once, and never shown again. Hashed with
    the same werkzeug function User.set_password already uses for the
    password itself, not a new hashing scheme.
    """

    __tablename__ = "recovery_code"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    code_hash = db.Column(db.String(255), nullable=False)
    # NULL means unused. Set the moment a code is spent at /login/verify-2fa
    # (or /account/2fa/disable) rather than deleting the row outright, so
    # "how many of my 10 are left" is a plain count query, not something
    # that has to be reconstructed from an audit log.
    used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        state = "used" if self.used_at else "unused"
        return f"<RecoveryCode user={self.user_id} {state}>"
