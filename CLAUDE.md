# Artha — project guide

For anyone (or any Claude Code session) picking this codebase up without prior
context. This captures decisions and conventions that aren't obvious from the
code alone — the code itself is usually well-commented on the *what*; this is
the *why*, and the standing rules that apply across the whole project rather
than one file.

## What Artha is

A personal finance and productivity "Personal OS" — Flask app, self-hosted,
no tracking. Live at arthaapp.com. Genuinely multi-user: roughly 8-10 real
people (friends and family, several outside the US) each running an
independent account with fully siloed data, not a shared household tool.

Built by Anwar Creative Studio (Tamkin Anwar), almost entirely with Claude
Code. The tracked GitHub history starts January 2026, but an untracked
offline predecessor goes back to June 2025 — don't describe the first git
commit as "when the project started."

## Product direction

Artha is built to be sellable and used by anyone, not just Tamkin — advise
and design for growing the real ~8-10 user base, not one person's workflow.
Each user's data is fully siloed (own transactions, own notes, own
scenarios). Don't propose shared/household features (joint budgets, split
expenses, shared views) unless explicitly asked; the single-owner data model
(`Transaction.user_id`, `Note.user_id`, etc.) is the intended product shape,
not a gap to fix.

## Architecture

- Flask + SQLAlchemy, one blueprint per feature area, under
  `artha/blueprints/`: `admin`, `ai`, `auth`, `dashboard`, `feedback`,
  `finance`, `notes`, `push`, `scenarios`, `search`.
- Models live in `artha/models/`, one file per model, exported through
  `__init__.py`.
- Money is always `db.Numeric` / `Decimal`, never `float` — an earlier
  version used floats and silently lost cents; that's fixed everywhere now,
  keep it that way in anything new.
- `artha/services/ai_service.py` owns every Anthropic API call. A lazy
  singleton client (`_get_client()`) avoids import-time failure when
  `ANTHROPIC_API_KEY` isn't set. Tool-use (`tools=[...]`, with `tool_choice`
  forcing a specific tool) is the pattern whenever the model needs to return
  typed data instead of prose — see `_STATEMENT_TOOL` and
  `_CATEGORIZE_TOOL` for two real examples beyond the chat assistant itself.
- The AI Assistant never writes to the database directly. It proposes an
  action (returned as `pending_actions`), the frontend renders a
  confirmation card, and only an explicit user click hits the real,
  already-validated write route (`/add_transaction` and similar). Any new
  AI-driven write needs to follow that same propose-then-confirm shape, not
  skip straight to writing.
- `Conversation` / `Message` models hold chat history server-side, not in
  client-side state, so the same conversation follows a user across
  devices.
- Bank statement import (`artha/blueprints/finance/routes.py`) is
  two-tiered: a fast, free, deterministic regex parser runs first and
  handles most real statements; an AI-assisted fallback
  (`_parse_statement_pdf_with_ai`, same tool-use pattern as above) only
  runs when the regex parser finds nothing at all, or leaves money-shaped
  lines it couldn't match sitting next to rows it did parse — and merges
  with the regex results (deduped by date + amount) rather than replacing
  them. Import categorization follows the identical two-tier shape: a free
  keyword list (`_CATEGORY_KEYWORDS`) first, an AI pass
  (`_fill_uncategorized_via_ai`) for whatever it misses, explicitly told to
  return nothing rather than guess when a description has no real signal.
- Tests: `pytest`, shared fixtures (`app`, `user`, `auth_client`) in
  `conftest.py`. Anything that calls the AI mocks
  `artha.services.ai_service._get_client` via `unittest.mock.patch` —
  tests never hit the real Anthropic API.

## Standing conventions

- **No em dashes, or any other stock AI-writing tell, in user-facing
  text** — transactional emails, in-app copy, flash/toast messages,
  README, GitHub description. Code comments and commit messages are
  exempt; the existing comment style already uses em dashes throughout
  and predates this rule. Grep any files touched by a task for the em dash
  character before calling it done if the task added user-facing copy.
- **Verify before flagging something as a bug.** This is a mature,
  actively-developed codebase — past design decisions are very often
  already explained in a comment directly above the code in question.
  Read that context before calling something broken, redundant, or a
  double-count in a review or audit.
- **Calculator (`templates/calculator.html`) phrasing gaps still get fixed
  by broadening the existing regex/pattern pipeline first**
  (`preprocess()`, `convertCurrencyPhrases()`,
  `convertLoanInterestPhrases()`, `stripMemoWords()`) — that offline path
  stays the default for anything a pattern can plausibly cover, kept
  instant and free. An AI fallback (`AIService.solve_calculator_line`,
  `/calculator/solve`) was added 2026-08-28, explicitly reconfirmed after
  the earlier no-AI decision, but only as a last resort: debounced
  client-side (900ms after the user stops typing, never per keystroke)
  and only for a line the deterministic pass either couldn't evaluate at
  all or flagged as untrustworthy (see `hasUnreliableImplicitMultiply` in
  that file). Don't widen when the AI fallback fires, or make it fire
  more eagerly, without asking again first — "offline handles it fast,
  AI only picks up the genuine leftovers" is the whole point.
- **Never commit or push without an explicit instruction to do so** —
  implement, test, verify live, then offer, and wait for a clear
  "commit and push" before running either command.
- **Any new `db.Boolean` column migration**: check the autogenerated
  migration file for `server_default=sa.text('1')` or `sa.text('0')`
  before considering it done, and replace with `sa.true()` / `sa.false()`.
  The `text()` form works fine against local SQLite but breaks the
  Postgres deploy (`psycopg2.errors.DatatypeMismatch`) — this has broken
  production twice already, and local `flask db upgrade` succeeding is
  not proof it's safe to ship.
- **Any new one-to-many relationship from `User`**: give it
  `cascade="all, delete-orphan"` explicitly, matching the existing
  `notes` / `transactions` relationships. `Event`, `PushSubscription`, and
  `Scenario` were missing this and silently orphaned rows on account
  deletion until it was caught and fixed (2026-08-27) — check any new
  child model for the same gap.
- **`templates/privacy.html` (`/privacy`, linked from Register and the
  Account menu) makes specific, checkable claims about how Artha handles
  data** — bank statements never touch disk, the AI only sees a snapshot
  and never writes directly, Anthropic doesn't train on API data, and so
  on. Any change to statement import, AI context/write access, hosting,
  or third-party services (added 2026-08-28) needs that page checked
  against the new reality and updated in the same change, not left to go
  stale. A wrong privacy claim users actually read is worse than no page
  at all.

## Known traps

- **A `hidden` attribute vs. a class's own `display` property.** An
  author stylesheet's `display` declaration always beats the browser's
  default `[hidden] { display: none }` at equal specificity, regardless
  of source order. Any element that's conditionally shown/hidden via the
  `hidden` attribute needs its visible-state CSS scoped to
  `:not([hidden])` (see `.csv-import-backdrop` in `templates/finance.html`
  for the documented pattern), or it renders regardless of the attribute.
  This has silently broken two different elements in that same file
  before being understood as a general trap, not a one-off. The same
  family of bug shows up with Tailwind's `hidden`/`md:block` responsive
  utility classes too: an inline `style="display:flex"` on the exact
  element carrying those classes beats them the same way, so anything
  meant to appear only above/below a breakpoint needs that `display`
  moved onto a wrapper element instead (see `settings-btn`'s
  `<div class="hidden md:block">` wrapper in `templates/base.html` for
  the pattern, and the "What's New" top-bar icon in that same file for
  a real instance of getting this wrong and then fixing it, 2026-08-29).
  A related but distinct variant of the same family: a `display:none`
  child isn't just visually hidden, it's excluded from CSS Grid's item
  generation and auto-placement entirely, so a sibling meant for the
  grid's *last* track can get auto-placed into an earlier one once the
  hidden item stops "holding its place." The top bar's
  `grid-template-columns:1fr auto 1fr` has three children (date, a
  `hidden sm:block` search bar, and the icons div); on mobile the search
  bar disappears, so the icons div auto-placed into column 2 instead of
  3, landing the mobile search icon stranded mid-bar instead of flush
  right (reported by a real user's screenshot, 2026-08-29). Fixed by
  pinning the surviving item's track explicitly (`grid-column:3` on the
  icons div in `templates/base.html`) instead of relying on
  auto-placement to skip the missing item correctly.
- **`scrollbar-width` vs. `::-webkit-scrollbar` on the same element.**
  Setting both makes Chrome/Safari/Edge silently prefer their own native
  "thin" scrollbar rendering over the custom webkit thumb styling, even
  though `scrollbar-width` alone is meant to be a Firefox-only concern.
  Gate it behind `@supports not selector(::-webkit-scrollbar)` — see
  `.scrollbar-thin` in `static/css/style.css`.
- **The dev server doesn't hot-reload Python or template changes** —
  stop and restart it after any backend/template edit before trusting a
  live check against it.
- **PWA / Service Worker caching.** `static/service-worker.js` used a
  cache-first strategy for static assets until 2026-08-28: since a
  CSS/JS-only deploy never changes `service-worker.js` itself, the
  browser had no reason to re-run its install step, so an already-
  visiting user's Service Worker could keep serving the exact response
  it cached the first time it ever ran, sometimes for weeks, surviving
  even a hard reload (the Service Worker intercepts the request before
  the browser's own cache-busting gets a say). This broke a real
  deploy in production, not just local testing, and is now
  network-first with a cache fallback for offline instead — routine
  CSS/JS changes reach an already-open tab on its next load without
  needing anything special. Still bump `CACHE_NAME` when changing
  `ASSETS_TO_CACHE` itself (adding/removing a cached file), and for a
  local check during this session, `navigator.serviceWorker.getRegistrations()`
  unregistered plus `caches.keys()` cleared remains the fastest way to
  rule the Service Worker out entirely if something still looks stale.

## Workflow for non-trivial changes

Research or root-cause the issue directly in the code (not by guessing) →
implement → write or update tests → full `pytest -q` run → live
verification (a throwaway test user, exercising the actual feature,
cleaned up afterward) → offer to commit → wait for an explicit go-ahead.
