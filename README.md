# Artha

A personal finance and productivity "Personal OS," built with Flask, designed as a fast, self-hosted alternative to bloated budgeting apps.

It focuses on clarity, speed, and control: self-hosted and no tracking, with an AI Assistant powered by Anthropic's API. Runs as a real multi-user app today — friends and family log in with their own accounts, not just a single-owner tool.

[Live app →](https://artha-dashboard.onrender.com)

## Features

**Finance**
- Five views on one page: Transactions, Spending, Income, Cash Flow, and Recurring
- Spending/Income/Cash Flow/Recurring all share the same flexible period picker — This Month (down to a specific past month), Last 3/6/12 Months, or a specific Year (year-to-date for the current year, full calendar year for past ones)
- Spending and Income break down by category in a live Chart.js doughnut; Cash Flow shows income vs. spending as a bar chart per period
- Recurring lists every recurring bill/income with a projected total scaled to the selected period
- Inline transaction editing, no page reloads; undo delete with toast notifications
- Bank statement import from CSV or PDF, with a preview-and-edit step and auto-categorization before anything is committed
- CSV export respecting whichever month filter is active
- Monthly spending budgets with a progress card and an over/near-limit alert
- Recurring transactions with automatic monthly regeneration

**Elsewhere**
- Notes with pinning, colors, tags, due dates, and a 30-day trash
- Calendar with due-date notes, recurring bill reminders, and time-blocked events
- Web Push notifications for bills and notes due today (opt-in, from Settings)
- A Numi-style smart calculator with variables, a running total, unit conversion, currency conversion via live exchange rates, and flexible natural-language input ("10k x 9%," "Rent 1800") — state persists across navigation
- Scenarios: model a "what if" financial decision (a new apartment, a career change) against your real numbers and get a payback period and a rule-based recommendation, no AI call needed
- An AI Assistant with your financial data as context, powered by Anthropic's API
- In-app feedback (floating button, any page) and an admin panel to triage it and see who's actually using the app
- An Account menu (your name/avatar) for identity actions — change password, sign out — separate from a Preferences menu (theme, currency, notifications)
- Dark and light theme with persistence, multi-currency display
- Mobile-first responsive design, tuned against real iOS and Android widths, not just "technically doesn't overflow"
- Offline support via Service Worker (PWA-ready)
- Rate-limited login and a pytest suite covering auth and the money-handling paths

## Tech stack
- Flask + SQLAlchemy
- Flask-Login + Flask-WTF (CSRF protection) + Flask-Limiter (login rate limiting)
- pdfplumber (bank statement PDF parsing)
- pywebpush (Web Push notifications)
- Vanilla JavaScript (ES modules), no frontend framework
- Chart.js
- Utility-first CSS (Tailwind) plus a small hand-written component layer
- Service Worker (offline caching, fallback, and push)
- pytest

## Why Artha
No SaaS lock-in, full control over data, and a real production-minded Flask app rather than a weekend prototype — built incrementally, for a real household of users, with clean Git history and feature isolation.

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
