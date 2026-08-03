# Artha

A personal finance and productivity dashboard built with Flask, designed as a fast, self-hosted alternative to bloated budgeting apps.

It focuses on clarity, speed, and control: self-hosted and no tracking, with an AI Assistant powered by Anthropic's API.

[Live app →](https://artha-dashboard.onrender.com)

## Features
- Income and expense tracking with live Chart.js visualizations
- Inline transaction editing, no page reloads
- Recurring transactions with automatic monthly regeneration
- Undo delete with toast notifications
- CSV export of transactions, respecting whichever month filter is active
- Monthly spending budgets with a progress card and an over/near-limit alert
- Notes with pinning, colors, tags, due dates, and checklists
- Calendar with due-date notes and recurring bill reminders
- Web Push notifications for bills due today (opt-in, from Settings)
- A Numi-style smart calculator with variables, a running total, unit conversion (lbs to kg, cm to m, ...), and currency conversion via live exchange rates
- Scenarios: model a "what if" financial decision (a new apartment, a career change) and get a payback period and a rule-based recommendation, no AI call needed
- An AI Assistant with your financial data as context, powered by Anthropic's API
- In-app feedback (floating button, any page) and an admin panel to triage it and see who's actually using the app
- Rate-limited login and a small pytest suite covering auth and the money-handling paths
- Dark and light theme with persistence
- Offline support via Service Worker (PWA-ready)

## Tech stack
- Flask + SQLAlchemy
- Flask-Login + Flask-WTF (CSRF protection) + Flask-Limiter (login rate limiting)
- pywebpush (Web Push notifications)
- Vanilla JavaScript (ES modules)
- Chart.js
- Utility-first CSS (Tailwind-style)
- Service Worker (offline caching, fallback, and push)
- pytest

## Why Artha
No SaaS lock-in, full control over data, and a real production-minded Flask app rather than a weekend prototype. Built incrementally with clean Git history and feature isolation.

## Running locally
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python init_db.py
python wsgi.py
```
`init_db.py` creates the local SQLite database and prompts for confirmation before running. The AI Assistant needs an `ANTHROPIC_API_KEY` environment variable to work; everything else runs without extra setup, including Web Push (a fixed dev-only VAPID keypair ships as a fallback; set real `VAPID_PUBLIC_KEY`/`VAPID_PRIVATE_KEY`/`VAPID_CLAIMS_EMAIL` env vars in any deployed environment instead).

`requirements-dev.txt` adds pytest on top of `requirements.txt`; production only needs the latter.

## Running tests
```bash
pytest
```

## Admin access
No account is an admin by default. Grant one with:
```bash
flask make-admin <username>
```

## Renewal reminders (Web Push)
Subscribing/receiving works from a normal page load, but *sending* a reminder on the actual due date needs to run even if nobody opens the app that day, and that can't happen from a request handler. Schedule this once a day (e.g. a Render Cron Job):
```bash
flask send-renewal-reminders
```
