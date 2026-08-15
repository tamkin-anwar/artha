from datetime import datetime, timezone

from ..extensions import db


class Note(db.Model):
    __tablename__ = "note"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=True)
    content = db.Column(db.Text, nullable=False)
    # Plain-text excerpt of content, derived server-side once at write time
    # (see artha.utils.derive_title_and_preview) — never re-derived
    # client-side, so the list view can render it directly.
    preview = db.Column(db.Text, nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0, index=True)
    pinned = db.Column(
        db.Boolean, nullable=False, default=False, server_default=db.text("0")
    )
    # Archived notes are excluded from the default /notes query entirely
    # (not just hidden client-side) — see notes.notes_page's ?view=archived
    # branch. Distinct from delete: archiving is meant to be reversible
    # indefinitely, not a 10-second undo window.
    archived = db.Column(
        db.Boolean, nullable=False, default=False, server_default=db.false()
    )
    # color: small fixed vocabulary (NOTE_COLORS in
    # artha.blueprints.notes.routes) rather than a DB enum, so the set can
    # change without an alter-type migration. tag: free text, normalized
    # (trimmed/lowercased) in that same module's _normalize_tag — not
    # validated against any fixed set. None means "unset" for both.
    color = db.Column(db.String(20), nullable=True)
    tag = db.Column(db.String(30), nullable=True)
    # Date only — the UI only ever shows/edits a date, never a time of day.
    due_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<Note {self.id}>"
