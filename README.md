# Artha Dashboard

Artha is a personal finance and productivity dashboard built with Flask, designed as a fast, self-hosted alternative to bloated budgeting apps.

It focuses on **clarity, speed, and control** — no accounts, no tracking, no third-party services.

---

## Features
- Income & expense tracking with live Chart.js visualizations
- Inline transaction editing (no page reloads)
- Undo delete with toast notifications
- Notes widget with autosave
- Calculator & calendar widgets
- Dark / light theme with persistence
- Offline support via Service Worker (PWA-ready)

---

## Tech Stack
- Flask + SQLAlchemy
- Flask-Login + Flask-WTF (CSRF protection)
- Vanilla JavaScript (ES modules)
- Chart.js
- Utility-first CSS (Tailwind-style)
- Service Worker (offline caching & fallback)

---

## Why Artha
- No SaaS lock-in  
- Full control over data  
- Designed as a real, production-minded Flask app  
- Built incrementally with clean Git history and feature isolation  

---

## Setup
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python wsgi.py