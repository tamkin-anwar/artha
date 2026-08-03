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
"""

import logging
from datetime import date

import click
from flask import current_app

from .blueprints.dashboard.routes import _next_due_date
from .extensions import db
from .models import PushSubscription, Transaction
from .services.push_service import send_push

log = logging.getLogger(__name__)


def _due_today_descriptions(user_id: int, today: date) -> list[str]:
    """Same dedup-by-(description,type)-then-_next_due_date() approach the
    dashboard's own "renewals this week" callout uses, narrowed to exactly
    today. Kept separate from that route rather than refactored to share
    code, since dashboard.index() needs the whole week and this needs one
    day — the two would diverge on the very first future tweak anyway."""
    recurring_rows = Transaction.query.filter_by(user_id=user_id, is_recurring=True).all()
    templates_by_key: dict[tuple[str, str], Transaction] = {}
    for t in recurring_rows:
        key = (t.description, t.type)
        current = templates_by_key.get(key)
        if current is None or (t.timestamp and current.timestamp and t.timestamp > current.timestamp):
            templates_by_key[key] = t

    due_today = []
    for (desc, _ttype), tx in templates_by_key.items():
        due = _next_due_date(tx, today)
        if due == today:
            due_today.append(desc)
    return due_today


def register_cli(app):
    @app.cli.command("send-renewal-reminders")
    def send_renewal_reminders():
        """Push one reminder to each subscribed user with a bill due today."""
        today = date.today()
        subs = PushSubscription.query.all()

        by_user: dict[int, list[PushSubscription]] = {}
        for sub in subs:
            by_user.setdefault(sub.user_id, []).append(sub)

        sent, skipped, pruned, failed = 0, 0, 0, 0

        for user_id, user_subs in by_user.items():
            descriptions = _due_today_descriptions(user_id, today)
            if not descriptions:
                continue

            if len(descriptions) == 1:
                body = f"{descriptions[0]} is due today."
            else:
                body = f"{len(descriptions)} bills due today: {', '.join(descriptions)}."

            for sub in user_subs:
                if sub.last_notified_date == today:
                    skipped += 1
                    continue

                result = send_push(sub, title="Artha", body=body, url="/")
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
