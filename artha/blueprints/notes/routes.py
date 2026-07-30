import time
import logging
from datetime import datetime

from flask import render_template, redirect, url_for, request, flash, session, jsonify
from flask_login import login_required, current_user
from sqlalchemy import func

from ...extensions import db
from ...models import Note
from ...utils import is_ajax_request, derive_title_and_preview
from . import notes_bp

log = logging.getLogger(__name__)

# Small fixed vocabularies for note.color / note.tag — enforced here rather
# than as a DB enum so the set can grow without an alter-type migration.
NOTE_COLORS = {"sage", "coral", "plum", "slate", "sky", "amber"}
NOTE_TAGS = {"personal", "ideas", "habits", "reading"}


def _serialize_note(note):
    return {
        "id": note.id,
        "title": note.title,
        "content": note.content,
        "preview": note.preview,
        "pinned": bool(note.pinned),
        "color": note.color,
        "tag": note.tag,
        "due_date": note.due_date.isoformat() if note.due_date else None,
    }


def _parse_due_date(raw):
    """Parse an ISO date string from the client, returning None for
    blank/missing input and silently ignoring malformed input — autosave
    calls are lenient by design (see update_note_fields), not a hard 400."""
    if raw in (None, ""):
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


@notes_bp.route("/notes", methods=["GET", "POST"])
@login_required
def notes_page():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        content = request.form.get("content", "").strip()

        if not content:
            flash("Note content is required.", "error")
            return redirect(url_for("notes.notes_page"))

        max_pos = (
            db.session.query(func.max(Note.position))
            .filter_by(user_id=current_user.id)
            .scalar()
            or 0
        )
        derived_title, preview = derive_title_and_preview(content)
        new_note = Note(
            title=title or derived_title,
            content=content,
            preview=preview,
            user_id=current_user.id,
            position=int(max_pos) + 1,
        )

        try:
            db.session.add(new_note)
            db.session.commit()
            flash("Note added!", "success")
        except Exception as e:
            db.session.rollback()
            log.error("Error adding note: %s", e, exc_info=True)
            flash("Error adding note", "error")

        return redirect(url_for("notes.notes_page"))

    notes = (
        Note.query.filter_by(user_id=current_user.id)
        # Newest first (by creation order) rather than oldest first —
        # matches Keep/Notes/Notion convention so a just-created note is
        # immediately visible without scrolling past everything else.
        .order_by(Note.pinned.desc(), Note.position.desc(), Note.id.desc())
        .all()
    )
    pinned_notes = [n for n in notes if n.pinned]
    other_notes = [n for n in notes if not n.pinned]
    return render_template(
        "notes.html",
        pinned_notes=pinned_notes,
        other_notes=other_notes,
        note_tags=sorted(NOTE_TAGS),
        note_colors=sorted(NOTE_COLORS),
    )


@notes_bp.route("/notes/new", methods=["POST"])
@login_required
def new_note():
    max_pos = (
        db.session.query(func.max(Note.position))
        .filter_by(user_id=current_user.id)
        .scalar()
        or 0
    )
    derived_title, preview = derive_title_and_preview("")
    note = Note(
        title=derived_title,
        content="",
        preview=preview,
        user_id=current_user.id,
        position=int(max_pos) + 1,
    )
    try:
        db.session.add(note)
        db.session.commit()
        return jsonify({
            **_serialize_note(note),
            "created_at": note.created_at.strftime("%b %d, %Y") if note.created_at else None,
        })
    except Exception as e:
        db.session.rollback()
        log.error("Error creating note: %s", e, exc_info=True)
        return jsonify({"message": "Error creating note"}), 500


@notes_bp.route("/notes/<int:note_id>", methods=["GET"])
@login_required
def get_note(note_id):
    note = db.session.get(Note, note_id)
    if note is None:
        return jsonify({"message": "Not found"}), 404
    if note.user_id != current_user.id:
        return jsonify({"message": "Unauthorized"}), 403

    return jsonify({
        **_serialize_note(note),
        "created_at": note.created_at.strftime("%b %d, %Y") if note.created_at else None,
    })


@notes_bp.route("/notes/<int:note_id>/update", methods=["PATCH"])
@login_required
def update_note_fields(note_id):
    note = db.session.get(Note, note_id)
    if note is None:
        return jsonify({"message": "Not found"}), 404
    if note.user_id != current_user.id:
        return jsonify({"message": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}

    # Auto-save is lenient by design — a blank title/content mid-edit is
    # not an error (unlike the older /update_note/<id> AJAX route, which
    # is a deliberate final-submit action that requires content).
    changed = False
    explicit_title = None
    if "title" in data:
        explicit_title = (data.get("title") or "").strip()
        changed = True
    if "content" in data:
        note.content = (data.get("content") or "").strip()
        changed = True

    # title/preview are derived once, here, from whatever content ends up
    # stored — never re-derived client-side. An explicit (non-blank) title
    # always wins; a blank title falls back to the auto-derived one, so
    # clearing the title field reverts to "first line of content" like
    # before, instead of leaving a stale title behind.
    if changed:
        derived_title, preview = derive_title_and_preview(note.content)
        note.title = explicit_title or derived_title
        note.preview = preview

    # pinned/color/tag/due_date each commit independently of title/content —
    # a pin toggle or color pick is very often sent alone, not bundled with
    # a text edit. Invalid color/tag values and malformed dates are ignored
    # rather than rejected with a 400, matching this endpoint's existing
    # "autosave is lenient by design" philosophy.
    if "pinned" in data:
        note.pinned = bool(data.get("pinned"))

    if "color" in data:
        color = data.get("color")
        if color is None or color in NOTE_COLORS:
            note.color = color

    if "tag" in data:
        tag = data.get("tag")
        if tag is None or tag in NOTE_TAGS:
            note.tag = tag

    if "due_date" in data:
        raw = data.get("due_date")
        if raw in (None, ""):
            note.due_date = None
        else:
            parsed = _parse_due_date(raw)
            if parsed is not None:
                note.due_date = parsed
            # malformed, non-blank input: ignore rather than clear an
            # existing due date the user didn't actually ask to remove

    try:
        db.session.commit()
        return jsonify({"message": "Note updated", **_serialize_note(note)})
    except Exception as e:
        db.session.rollback()
        log.error("Error auto-saving note: %s", e, exc_info=True)
        return jsonify({"message": "Database error"}), 500


@notes_bp.route("/update_note/<int:note_id>", methods=["POST"])
@login_required
def update_note(note_id):
    note = db.session.get(Note, note_id)
    if note is None:
        return jsonify({"message": "Not found"}), 404
    if note.user_id != current_user.id:
        return jsonify({"message": "Unauthorized"}), 403

    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"message": "Empty content"}), 400

    note.content = content
    try:
        db.session.commit()
        return jsonify({"message": "Note updated"})
    except Exception as e:
        db.session.rollback()
        log.error("Error updating note: %s", e, exc_info=True)
        return jsonify({"message": "Database error"}), 500


@notes_bp.route("/reorder_notes", methods=["POST"])
@login_required
def reorder_notes():
    data = request.get_json(silent=True) or {}
    order = data.get("order")

    if not isinstance(order, list) or not order:
        return jsonify({"message": "Invalid order payload."}), 400

    try:
        ids = [int(x) for x in order]
    except Exception:
        return jsonify({"message": "Order must be a list of integers."}), 400

    notes = Note.query.filter(
        Note.user_id == current_user.id, Note.id.in_(ids)
    ).all()

    if {n.id for n in notes} != set(ids):
        return jsonify({"message": "Order contains unknown or unauthorized note ids."}), 403

    # notes_page() sorts position DESC (newest/most-recently-arranged
    # first), so the first id in the submitted order — the top card —
    # must get the *highest* position, not the lowest.
    id_to_note = {n.id: n for n in notes}
    for idx, note_id in enumerate(ids):
        id_to_note[note_id].position = len(ids) - idx

    try:
        db.session.commit()
        return jsonify({"message": "Note order saved."})
    except Exception as e:
        db.session.rollback()
        log.error("Error saving note order: %s", e, exc_info=True)
        return jsonify({"message": "Database error"}), 500


@notes_bp.route("/delete_note/<int:note_id>", methods=["POST"])
@login_required
def delete_note(note_id):
    note = db.session.get(Note, note_id)
    if note is None:
        if is_ajax_request():
            return jsonify({"message": "Not found"}), 404
        flash("Note not found", "error")
        return redirect(url_for("dashboard.index"))

    if note.user_id != current_user.id:
        if is_ajax_request():
            return jsonify({"message": "Unauthorized"}), 403
        flash("Unauthorized action", "error")
        return redirect(url_for("dashboard.index"))

    session["last_deleted_note"] = {
        "user_id": note.user_id,
        "title": note.title,
        "content": note.content,
        "preview": note.preview,
        "position": int(note.position or 0),
        "pinned": bool(note.pinned),
        "color": note.color,
        "tag": note.tag,
        "due_date": note.due_date.isoformat() if note.due_date else None,
        "deleted_at": time.time(),
    }

    try:
        db.session.delete(note)
        db.session.commit()
        if is_ajax_request():
            return jsonify({"message": "Note deleted", "can_undo": True})
        flash("Note deleted.", "success")
        return redirect(url_for("dashboard.index"))
    except Exception as e:
        db.session.rollback()
        log.error("Error deleting note: %s", e, exc_info=True)
        if is_ajax_request():
            return jsonify({"message": "Error deleting note"}), 500
        flash("Error deleting note", "error")
        return redirect(url_for("dashboard.index"))


@notes_bp.route("/undo_delete_note", methods=["POST"])
@login_required
def undo_delete_note():
    data = session.get("last_deleted_note")

    if not data or data.get("user_id") != current_user.id:
        return jsonify({"message": "Nothing to undo."}), 400

    if time.time() - float(data.get("deleted_at", 0)) > 10:
        session.pop("last_deleted_note", None)
        return jsonify({"message": "Undo window expired."}), 400

    try:
        restored_pos = int(data.get("position") or 0)
        if restored_pos <= 0:
            max_pos = (
                db.session.query(func.max(Note.position))
                .filter_by(user_id=current_user.id)
                .scalar()
                or 0
            )
            restored_pos = int(max_pos) + 1
        else:
            Note.query.filter(
                Note.user_id == current_user.id,
                Note.position >= restored_pos,
            ).update({Note.position: Note.position + 1}, synchronize_session=False)

        restored = Note(
            title=data.get("title"),
            content=data["content"],
            preview=data.get("preview"),
            user_id=current_user.id,
            position=restored_pos,
            pinned=bool(data.get("pinned")),
            color=data.get("color"),
            tag=data.get("tag"),
            due_date=_parse_due_date(data.get("due_date")),
        )
        db.session.add(restored)
        db.session.commit()
        session.pop("last_deleted_note", None)

        return jsonify({
            "message": "Note restored.",
            **_serialize_note(restored),
            "created_at": restored.created_at.strftime("%b %d, %Y") if restored.created_at else None,
        })
    except Exception as e:
        db.session.rollback()
        log.error("Error undoing note delete: %s", e, exc_info=True)
        return jsonify({"message": "Error restoring note"}), 500
