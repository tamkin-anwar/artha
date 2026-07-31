# Artha

A personal finance and productivity dashboard built with Flask, designed as a fast, self-hosted alternative to bloated budgeting apps.

It focuses on clarity, speed, and control: no accounts, no tracking, no third-party services.

[Live app →](https://artha-dashboard.onrender.com)

## Features
- Income and expense tracking with live Chart.js visualizations
- Inline transaction editing, no page reloads
- Undo delete with toast notifications
- Notes with pinning, colors, tags, due dates, and checklists
- Calendar with due-date notes and recurring bill reminders
- A Numi-style smart calculator with variables and a running total
- Dark and light theme with persistence
- Offline support via Service Worker (PWA-ready)

## Tech stack
- Flask + SQLAlchemy
- Flask-Login + Flask-WTF (CSRF protection)
- Vanilla JavaScript (ES modules)
- Chart.js
- Utility-first CSS (Tailwind-style)
- Service Worker (offline caching and fallback)

## Why Artha
No SaaS lock-in, full control over data, and a real production-minded Flask app rather than a weekend prototype. Built incrementally with clean Git history and feature isolation.

## Running locally
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python wsgi.py
```
