"""
artha/cli.py
-------------
`flask send-renewal-reminders` — the proactive half of push notifications.

Web Push subscribing/receiving works from a normal request, but *sending*
a reminder on the actual due date has to happen even when nobody's
opened the app that day, so it can't live inside any view function. This
command is meant to be triggered once an HOUR by an external scheduler
(a Render Cron Job hitting `flask send-renewal-reminders`, `0 * * * *`)
rather than anything in-app — hourly, not daily, since each user now
picks their own local delivery hour (User.reminder_hour) rather than
everyone getting one fixed server-side time; the command itself decides
per run which users' local clock currently matches their own preference
and skips everyone else. (This changed from a daily to an hourly
schedule when per-user reminder times shipped — the external Render Cron
Job's schedule has to be updated by hand alongside this deploy, same as
the command's own history already required for other reasons: see the
naming note below.)

Covers three kinds of "due today": recurring bills (same logic as the
dashboard's renewals callout), Notes with a due_date of today, and
calendar Events starting today. The command is still named
send-renewal-reminders rather than something more generic, since that's
already the exact string wired into the deployed Render Cron Job's
command; renaming it would silently break that schedule until someone
noticed and updated it there too.

Both "today" and "the current hour" are computed per user, in that
user's own timezone (utils.user_today/user_now) — not the server's UTC
clock. Before this, `today = date.today()` was one single UTC date
shared by every user regardless of where they actually are, which is
the same class of day-boundary bug already found and fixed elsewhere in
this app: for anyone not physically in UTC, "due today" could silently
mean yesterday or tomorrow depending on the time of day this command
happened to run.
"""

import logging
from datetime import date, datetime, timedelta, timezone

import click
from flask import current_app

from .blueprints.dashboard.routes import _generate_recurring_events
from .blueprints.notes.routes import TRASH_RETENTION_DAYS
from .extensions import db
from .models import Event, Note, PushSubscription, Transaction, User
from .services.push_service import send_push
from .utils import next_due_date, user_now, user_today

log = logging.getLogger(__name__)

# Fallback delivery hour (in the user's own local time) for anyone who
# hasn't picked one yet in Settings — matches the fixed hour this command
# used to run at for everyone, before User.reminder_hour existed, so an
# account that's never touched the new setting keeps the same behavior
# it always had.
DEFAULT_REMINDER_HOUR = 8


def _due_today_items(
    user_id: int,
    today: date,
    notify_bills: bool = True,
    notify_notes: bool = True,
    notify_events: bool = True,
) -> list[str]:
    """Everything worth a daily nudge for this user, as one flat list of
    display strings: recurring bills whose next occurrence lands today
    (same dedup-by-(description,type)-then-next_due_date() approach the
    dashboard's own "renewals this week" callout uses), Notes due today,
    and calendar Events starting today. The notification doesn't need to
    distinguish *why* something's due, just *what* is, so all three feed
    the same list rather than being tracked and formatted separately.

    notify_bills/notify_notes/notify_events gate each third independently
    per the user's own preference (User.notify_bills_due/notify_notes_due/
    notify_events_due) — all three default True so every existing caller/
    test keeps seeing today's behavior unchanged."""
    items = []

    if notify_bills:
        recurring_rows = Transaction.query.filter_by(user_id=user_id, is_recurring=True).all()
        templates_by_key: dict[tuple[str, str], Transaction] = {}
        for t in recurring_rows:
            key = (t.description, t.type)
            current = templates_by_key.get(key)
            if current is None or (t.timestamp and current.timestamp and t.timestamp > current.timestamp):
                templates_by_key[key] = t

        for (desc, _ttype), tx in templates_by_key.items():
            due = next_due_date(tx, today)
            if due == today:
                items.append(desc)

    if notify_notes:
        # Archived notes are done/put-away by definition — they shouldn't nag.
        notes_due_today = Note.query.filter_by(
            user_id=user_id, due_date=today, archived=False
        ).all()
        for note in notes_due_today:
            items.append(note.title or (note.preview[:40] if note.preview else "Untitled note"))

    if notify_events:
        today_start_dt = datetime(today.year, today.month, today.day)
        today_end_dt = today_start_dt + timedelta(days=1)
        # Recurring event occurrences only become real Event rows once
        # something queries their window (see _generate_recurring_events's
        # own docstring) — normally that's the calendar page loading, but
        # this command runs on a schedule with nobody around to trigger
        # that, so it has to do the same materialization itself first, or
        # a recurring event's "today" occurrence could simply not exist
        # yet for this query to find.
        _generate_recurring_events(user_id, today_start_dt, today_end_dt)
        events_today = Event.query.filter(
            Event.user_id == user_id,
            Event.start >= today_start_dt,
            Event.start < today_end_dt,
        ).all()
        for event in events_today:
            items.append(event.title)

    return items


def register_cli(app):
    @app.cli.command("send-renewal-reminders")
    def send_renewal_reminders():
        """Push one reminder to each subscribed user with a bill, note, or
        event due today, IF the current hour in that user's own timezone
        matches their own chosen reminder_hour (or DEFAULT_REMINDER_HOUR if
        they've never set one). Meant to run once an hour, not once a day —
        see this module's own docstring for why."""
        subs = PushSubscription.query.all()

        by_user: dict[int, list[PushSubscription]] = {}
        for sub in subs:
            by_user.setdefault(sub.user_id, []).append(sub)

        sent, skipped, pruned, failed, not_their_hour = 0, 0, 0, 0, 0

        for user_id, user_subs in by_user.items():
            owner = db.session.get(User, user_id)
            if owner is None:
                continue

            now = user_now(owner)
            wanted_hour = owner.reminder_hour if owner.reminder_hour is not None else DEFAULT_REMINDER_HOUR
            if now.hour != wanted_hour:
                not_their_hour += len(user_subs)
                continue

            today = now.date()
            items = _due_today_items(
                user_id,
                today,
                owner.notify_bills_due,
                owner.notify_notes_due,
                owner.notify_events_due,
            )
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
            f"{not_their_hour} not their hour yet, "
            f"pruned {pruned} dead subscriptions, {failed} failed (will retry next run)."
        )

    @app.cli.command("purge-expired-trash")
    def purge_expired_trash():
        """Permanently delete notes that have sat in Trash past
        TRASH_RETENTION_DAYS, across all users.

        Not strictly required: notes.routes._purge_expired_trash already
        does this lazily for a given user on every /notes page load, so
        the 30-day promise holds on its own for anyone who reopens Notes.
        This command exists for the same reason send-renewal-reminders
        does — an optional daily Render Cron Job (`flask
        purge-expired-trash`) gives a hard guarantee even for accounts
        that never revisit Notes long enough to trigger the lazy sweep.
        """
        cutoff = datetime.utcnow() - timedelta(days=TRASH_RETENTION_DAYS)
        expired = Note.query.filter(
            Note.deleted_at.isnot(None), Note.deleted_at < cutoff
        ).all()
        count = len(expired)
        for note in expired:
            db.session.delete(note)
        db.session.commit()
        click.echo(f"Purged {count} note(s) from trash.")

    @app.cli.command("purge-expired-accounts")
    def purge_expired_accounts():
        """Permanently delete accounts whose requested deletion passed
        ACCOUNT_DELETION_GRACE_DAYS ago.

        Not strictly required: blueprints.auth.routes._purge_expired_deleted_accounts
        already does this lazily on every /login page load, so the 30-day
        promise holds on its own site-wide traffic alone. This command
        exists for the same reason purge-expired-trash does — a daily
        Render Cron Job (`flask purge-expired-accounts`) gives a hard
        guarantee that doesn't depend on someone else happening to load
        /login. Unlike purge-expired-trash's bulk delete, this deletes
        User rows one at a time (db.session.delete(), not Query.delete())
        so every ORM-level cascade="all, delete-orphan" relationship on
        User actually fires — a bulk delete would bypass all of them and
        orphan a user's transactions, notes, events, and everything else.
        """
        from .blueprints.auth.routes import ACCOUNT_DELETION_GRACE_DAYS

        cutoff = datetime.now(timezone.utc) - timedelta(days=ACCOUNT_DELETION_GRACE_DAYS)
        expired = User.query.filter(
            User.deleted_at.isnot(None), User.deleted_at < cutoff
        ).all()
        count = len(expired)
        for user in expired:
            db.session.delete(user)
        db.session.commit()
        click.echo(f"Purged {count} account(s) past their deletion grace period.")
