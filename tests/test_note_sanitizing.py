"""
Note HTML is rendered back as live HTML, so it's sanitized (utils.
sanitize_note_html, nh3) on every save and every read -- while everything
the editor itself produces, checklists especially, survives byte for byte.
"""

from artha.extensions import db
from artha.models import Note
from artha.utils import sanitize_note_html

AJAX = {"X-Requested-With": "XMLHttpRequest"}

CHECKLIST = (
    '<div class="note-checklist-item checked"><input type="checkbox" aria-label="Toggle checklist item" '
    'contenteditable="false" checked=""><span class="note-checklist-text">​Milk</span></div>'
    '<div class="note-checklist-item"><input type="checkbox" aria-label="Toggle checklist item" '
    'contenteditable="false"><span class="note-checklist-text">​Eggs</span></div>'
)
FORMATTING = '<h2>Plan</h2><div><b>bold</b> <i>it</i> <u>u</u> <strike>s</strike> <code>c</code></div><ul><li>a</li></ul>'


def test_editor_output_survives_unchanged():
    assert sanitize_note_html(CHECKLIST) == CHECKLIST
    assert sanitize_note_html(FORMATTING) == FORMATTING


def test_script_and_event_handler_vectors_are_stripped():
    dirty = (
        '<img src=x onerror=alert(1)><script>alert(2)</script>'
        '<a href="javascript:alert(3)">x</a><div onclick="alert(4)">y</div>'
        '<iframe src="https://evil.example"></iframe>'
    )
    clean = sanitize_note_html(dirty)
    for bad in ("onerror", "<script", "javascript:", "onclick", "<iframe", "<img"):
        assert bad not in clean


def test_plain_text_is_left_alone():
    # Not HTML at all -- escaping it here would double-escape it later.
    assert sanitize_note_html("a < b & c") == "a < b & c"


def test_only_a_checkbox_input_keeps_its_type():
    assert 'type="text"' not in sanitize_note_html('<div><input type="text" value="x"></div>')


def test_autosave_sanitizes_content(auth_client, user):
    note = Note(user_id=user.id, title="t", content="", position=1)
    db.session.add(note)
    db.session.commit()

    auth_client.patch(
        f"/notes/{note.id}/update",
        json={"content": "<div>hi<img src=x onerror=alert(1)></div>"},
        headers=AJAX,
    )

    db.session.refresh(note)
    assert "onerror" not in note.content
    assert "hi" in note.content


def test_a_note_saved_before_sanitizing_is_cleaned_on_the_way_out(auth_client, user):
    note = Note(user_id=user.id, title="old", content="<div>x<script>alert(1)</script></div>", position=1)
    db.session.add(note)
    db.session.commit()

    data = auth_client.get(f"/notes/{note.id}", headers=AJAX).get_json()
    page = auth_client.get("/notes").get_data(as_text=True)

    assert "<script>alert(1)" not in data["content"]
    assert "<script>alert(1)" not in page
