"""
artha/cli.py
-------------
`flask send-renewal-reminders` — the proactive half of push notifications.

Web Push subscribing/receiving works from a normal request, but *sending*
a reminder on the actual due date has to happen even when nobody's
opened the app that day, so it can't live inside any view function. This
command is meant to be triggered once a day by an external scheduler
(a Render Cron Job hitting `flask send-renewal-reminders`, e.g. daily at
8am) rather than anything in-app.

Covers two kinds of "due today": recurring bills (same logic as the
dashboard's renewals callout) and Notes with a due_date of today. The
command is still named send-renewal-reminders rather than something more
generic, since that's already the exact string wired into the deployed
Render Cron Job's command; renaming it would silently break that
schedule until someone noticed and updated it there too.
"""

import logging
from datetime import date

import click
from flask import current_app

from .blueprints.dashboard.routes import _next_due_date
from .extensions import db
from .models import Note, PushSubscription, Transaction
from .services.push_service import send_push

log = logging.getLogger(__name__)


def _due_today_items(user_id: int, today: date) -> list[str]:
    """Everything worth a daily nudge for this user, as one flat list of
    display strings: recurring bills whose next occurrence lands today
    (same dedup-by-(description,type)-then-_next_due_date() approach the
    dashboard's own "renewals this week" callout uses), plus Notes due
    today. The notification doesn't need to distinguish *why* something's
    due, just *what* is, so both feed the same list rather than being
    tracked and formatted separately."""
    recurring_rows = Transaction.query.filter_by(user_id=user_id, is_recurring=True).all()
    templates_by_key: dict[tuple[str, str], Transaction] = {}
    for t in recurring_rows:
        key = (t.description, t.type)
        current = templates_by_key.get(key)
        if current is None or (t.timestamp and current.timestamp and t.timestamp > current.timestamp):
            templates_by_key[key] = t

    items = []
    for (desc, _ttype), tx in templates_by_key.items():
        due = _next_due_date(tx, today)
        if due == today:
            items.append(desc)

    # Archived notes are done/put-away by definition — they shouldn't nag.
    notes_due_today = Note.query.filter_by(
        user_id=user_id, due_date=today, archived=False
    ).all()
    for note in notes_due_today:
        items.append(note.title or (note.preview[:40] if note.preview else "Untitled note"))

    return items


def register_cli(app):
    @app.cli.command("send-renewal-reminders")
    def send_renewal_reminders():
        """Push one reminder to each subscribed user with a bill or note due today."""
        today = date.today()
        subs = PushSubscription.query.all()

        by_user: dict[int, list[PushSubscription]] = {}
        for sub in subs:
            by_user.setdefault(sub.user_id, []).append(sub)

        sent, skipped, pruned, failed = 0, 0, 0, 0

        for user_id, user_subs in by_user.items():
            items = _due_today_items(user_id, today)
            if not items:
                continue

            if len(items) == 1:
                body = f"{items[0]} is due today."
            else:
                body = f"{len(items)} things due today: {', '.join(items)}."

            for sub in user_subs:
                if sub.last_notified_date == today:
                    skipped += 1
                    continue

                result = send_push(sub, title="Due today", body=body, url="/")
                if result == "sent":
                    sub.last_notified_date = today
                    sent += 1
                elif result == "gone":
                    db.session.delete(sub)
                    pruned += 1
                else:
                    failed += 1

        db.session.commit()
        click.echo(
            f"Sent {sent}, skipped {skipped} (already notified today), "
            f"pruned {pruned} dead subscriptions, {failed} failed (will retry next run)."
        )
