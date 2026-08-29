"""
artha/changelog.py
--------------------
Static list of user-facing entries for /whats-new (blueprints/dashboard/
routes.py). Plain, short, benefit-focused language: what changed and
why it matters to someone using the app, not the engineering detail
behind it. That fuller story lives in the Artha Logbook, a separate
document written for a different audience, not something the app itself
points users to.

Add one entry here whenever something ships that a user would actually
notice, in the same change that ships it, not after. A fix nobody would
ever see doesn't need one. Keep each body to a sentence or two.

Fields: date (YYYY-MM-DD, the day it shipped), category ("new" |
"improved" | "fixed"), title, body. Newest first -- this list is the
render order too.
"""

CHANGELOG_ENTRIES = [
    {
        "date": "2026-08-29",
        "category": "new",
        "title": "See what's new, right here",
        "body": "A running list of what's changed in Artha, in plain language, no digging through commit history required. Check back whenever you're curious.",
    },
    {
        "date": "2026-08-28",
        "category": "improved",
        "title": "A calculator that tells you when it's unsure",
        "body": "Real word problems, like \"split a $60 bill 3 ways with 18% tip\", now get solved. When a line genuinely can't be answered, the calculator says so instead of staying silently blank.",
    },
    {
        "date": "2026-08-28",
        "category": "new",
        "title": "Privacy & Security, explained plainly",
        "body": "A new page laying out exactly where your data lives, how your account is protected, and what happens to a bank statement the moment you upload one. Find it under your account menu.",
    },
    {
        "date": "2026-08-27",
        "category": "new",
        "title": "Two-factor authentication and account deletion",
        "body": "Lock your account down with a code from an authenticator app, on top of your password. And if you ever want to leave, deleting your account is now a real, self-serve option, with a 30-day window to change your mind.",
    },
    {
        "date": "2026-08-27",
        "category": "fixed",
        "title": "Smoother scrolling, better mobile layout",
        "body": "Cleaned up inconsistent scrollbars across the app and fixed several mobile layout issues, including a global search that was unreachable on small screens.",
    },
]
