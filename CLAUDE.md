# Artha — project guide

For anyone (or any Claude Code session) picking this codebase up without prior
context. This captures decisions and conventions that aren't obvious from the
code alone — the code itself is usually well-commented on the *what*; this is
the *why*, and the standing rules that apply across the whole project rather
than one file.

## What Artha is

A personal finance and productivity "Personal OS" — Flask app, self-hosted,
no tracking. Live at arthaapp.com. Genuinely multi-user: 20+ real people
(as of 2026-10-05; friends and family, several outside the US) each running an
independent account with fully siloed data, not a shared household tool.

Built by Anwar Creative Studio (Tamkin Anwar), almost entirely with Claude
Code. The tracked GitHub history starts January 2026, but an untracked
offline predecessor goes back to June 2025 — don't describe the first git
commit as "when the project started."

## Product direction

Artha is built to be sellable and used by anyone, not just Tamkin — advise
and design for growing the real 20+ user base, not one person's workflow.
Each user's data is fully siloed (own transactions, own notes, own
scenarios). Don't propose shared/household features (joint budgets, split
expenses, shared views) unless explicitly asked; the single-owner data model
(`Transaction.user_id`, `Note.user_id`, etc.) is the intended product shape,
not a gap to fix.

## Competitive ambition (added 2026-09-22)

Long-term goal, not a one-off task: Artha should become one of the
genuinely top personal finance apps, and specifically the most
beautiful app on iOS — not just the most beautiful *finance* app.
Judge visual/interaction work against Apple Design Award criteria
(Interaction: intuitive, effortless controls tailored to the
platform; Visuals and Graphics: cohesive theme, polished animation),
not "does this look fine for a budgeting app."

Grounded in researched 2026 user sentiment on the three apps most
worth learning from — steal what people love, skip what they don't:

- **Copilot Money** — the design bar to chase: typography/motion/
  color/information-density polish that feels like a native app, not
  a fintech dashboard; AI categorization that "trains" on the user's
  own corrections and remembers them per-merchant (Artha's two-tier
  keyword-then-AI categorizer is the seed of this — extending it to
  remember a user's own per-merchant corrections, not just the global
  keyword list, is the natural next step). Avoid its real complaints:
  iOS-only (alienates the other half of a household — worth
  remembering given the Swift-app note below), no free tier with a
  card-required trial that silently auto-converts, weak multi-
  currency/international support (Artha already has this — protect
  it, don't regress it chasing Copilot's polish), and "training" the
  categorizer feeling like unpaid setup work instead of something
  that happens invisibly in the background.
- **Monarch Money** — most-loved for being a genuine all-in-one
  dashboard (net worth trajectory across accounts, not just this
  month's budget) and for doing couples/shared access well ("two
  apps for the price of one"). That couples angle directly conflicts
  with this doc's existing single-owner-by-design rule above — don't
  silently build toward it; it needs an explicit product decision
  from Tamkin first, never an assumption. Avoid its complaints:
  brittle bank-sync (Plaid re-auth breaking on smaller institutions)
  and a short trial. Artha has no live bank sync at all (manual entry
  + one-shot statement import) — that's an existing structural
  immunity to this whole complaint category, not a gap to fix by
  bolting on fragile sync.
- **YNAB** — most-loved for genuinely changing behavior (real
  debt-payoff stories), because it's proactive, not passive. Worth
  stealing the *spirit* (nudges, a clear "here's what's safe to
  spend" figure) without the complaints: a steep mandatory learning
  curve, forced constant active engagement, and "improvements" that
  added complexity long-time users never asked for. Artha should stay
  usable passively (quick-add, auto-categorize) while offering deeper
  engagement as opt-in, never required.

**Platform decision (researched and settled 2026-09-22): the web app
does not get rewritten or re-platformed to chase "most beautiful."**
Researched what Copilot and Monarch are actually built with: Copilot
(the app Tamkin named as the bar) is fully native Swift/UIKit, with
newer features (Cash Flow) in SwiftUI/Swift Charts — its macOS app is
the same Swift codebase with a few Mac-specific views swapped in.
Monarch is React Native + Python/Django — well-regarded, but it's not
the app people call "the most beautiful," Copilot is. The conclusion
that follows: Copilot's polish (physics-based animation, native
gestures, ProMotion-smooth scrolling, haptics, native widgets) comes
from native platform primitives a browser cannot fully replicate —
rewriting this Flask/Jinja web app into a different web stack (React
or otherwise) would not close that gap, since the result is still a
website in a browser either way. It would also be a real-user-facing
risk for a live app with 20+ people's actual financial data, for a
reward that doesn't reach the stated goal. Confirmed directly with
Tamkin after laying out this reasoning — keep the web app on
Flask/Jinja/vanilla JS, keep polishing it (the batch-based UI/UX work
already underway), and treat "most beautiful app on iOS" as solved by
an eventual **separate, additive native Swift/SwiftUI app**, not a
web rewrite. **That app started on 2026-10-19** as the `artha-apple`
repo (one SwiftUI codebase for iPhone and iPad, Mac later; Windows and
the browser stay on this web app). Its first API is this repo's
`api` blueprint; see Architecture below.

**When the native Swift app project actually starts**, do not
re-research or re-litigate this decision — go straight to building
per [[project_artha_competitive_ambition]] (auto-memory, not this
repo), which holds the fuller context: which specific things to steal
from Copilot vs. avoid, that SwiftUI (not UIKit) is the right choice
since it's Apple's actively-invested-in framework and is what
Copilot's own newer work already moved to, that this is a new client
built against the existing Flask backend as its API (not a port of
the Jinja templates), and that the web app stays a first-class
citizen alongside it rather than being sunset — Copilot's own biggest
user complaint is being iOS-only and shutting out non-Apple users,
and Artha shouldn't repeat that.

## Architecture

- Flask + SQLAlchemy, one blueprint per feature area, under
  `artha/blueprints/`: `accounts`, `admin`, `ai`, `api`, `auth`,
  `dashboard`, `feedback`, `finance`, `notes`, `push`, `scenarios`,
  `search`.
- **`api` is the native app's JSON API** (`/api/v1/...`, added
  2026-10-20 for the SwiftUI app in the separate `artha-apple` repo).
  It authenticates with a per-device bearer token (`models/api_token.py`,
  stored only as a SHA-256 hash), never the web session cookie, which is
  why the whole blueprint is CSRF-exempt; a web session cookie does not
  authenticate it either (tested). Errors on `/api/v1/` paths are always
  JSON `{"error": code, "message": ...}`, including 404/405/429/500 (see
  `create_app`). Money is serialized as two-decimal strings, never floats.
  Changing or resetting a password, or requesting account deletion,
  revokes every token (`ApiToken.revoke_all_for`). **The API reuses the
  web app's logic instead of reimplementing it**: Today comes from
  `services/month_snapshot.py`'s `build_month_snapshot` (the Dashboard's
  Safe to Spend math, extracted so both use one copy), and adding,
  editing and deleting a transaction go through `finance.routes`'
  `create_transaction`, `update_transaction_fields` and
  `delete_transaction_record` (the same validation, currency lock,
  categorizing, remembered corrections and budget alerts as the web
  app). The app's Plan tab (`/plan`) is `services/month_plan.py` on top
  of the same snapshot (its "left to spend" is Safe to Spend in parts),
  and budgets save through `save_budget` / `save_category_budget` /
  `remove_category_budget`, shared with the web forms. The Wealth tab
  (`/wealth`, `/accounts`) saves through `accounts.routes`' `save_account`
  / `delete_account_record` (shared with the web Accounts page; the app
  sends only changed fields, and only a sent balance moves
  `balance_updated_at`). Signing up (`POST /users`) and deleting an
  account (`DELETE /me`) go through `auth.routes`' `register_user` /
  `schedule_account_deletion`, shared with the web Register and Edit
  Profile forms; the app needs both because App Store guideline 5.1.1(v)
  requires in-app deletion for any app that can create an account.
  `/.well-known/apple-app-site-association` (`create_app`) links
  arthaapp.com's saved passwords to the app (`webcredentials` only).
  Settings (`PATCH /me`, `POST /me/password`, `/me/two-factor`) go
  through `auth.routes`' `update_profile`, `change_user_password` and the
  two-factor functions, shared with the web Edit Profile, Change Password,
  2FA and currency forms; a password change revokes every token, so the
  API hands the calling device a fresh one.
  `Transaction.needs_review` marks an Apple Pay purchase the app logged on
  its own until the person confirms it (`POST /transactions/reviewed`) or
  edits it (`update_transaction_fields` clears it); statement imports
  never set it, since every row there is reviewed before saving.
  New endpoints should follow that rule: extract the web route's
  logic into a shared function and call it from both, never copy it.
- **A tracked bill and its real charge count once**
  (`services/bill_matching.py`, 2026-10-22). Bill series are keyed by
  exact description, so Apple Pay's "NETFLIX.COM" or a statement line
  used to count again beside the generated "Netflix" row. Every real
  charge (`create_transaction`, the import commit) is first offered to
  `find_open_occurrence`, which merges it into an open bill row of the
  same type and currency in the same month (within 10 days for monthly,
  2 weekly, 4 biweekly) when a similar name is within 10%, or the exact
  amount matches and has cents (a round $60.00 alone matched a $60 Zelle
  to someone else in the first real cleanup run, so it needs the name); `generate_due_recurring` likewise adopts an earlier
  charge (`find_charge_for`) instead of adding a copy. Ties never match.
  `Transaction.bill_matched` marks an occurrence a charge already landed
  on, and `Transaction.occurrence_day` keeps a weekly bill's due date
  when its charge lands a day off; Safe to Spend and Plan's "posted?"
  checks use it.
  Every pairing can be undone (`bill_matching.unpair`, YNAB's Unmatch):
  `absorb_charge` / `adopt_charge` save what the bill and the charge each
  were in `Transaction.pairing` (JSON), and `unpair_transaction_record`
  (web `POST /activity/<id>/unpair`, API `POST /transactions/<id>/unpair`)
  puts both back, the bill as planned and the charge under its own name,
  marked `bill_match_declined` so it's never paired on its own again.
  Rows paired before 2026-10-23 have no `pairing` and can't be split; the
  serializer's `paired_charge` is null for them.
  When it misses one, the person pairs it by hand (`bills_for` /
  `pair_by_hand`, web `GET /activity/<id>/bills` + `POST .../pair`, API
  `/transactions/<id>/bills` + `/pair`, shared through
  `finance.routes.bill_choices` / `pair_transaction_record`): open bill
  occurrences of the same type and currency within 25 days, a name match
  first, then the closest amount and date. It merges exactly like an
  automatic pairing, so Unpair works on it, and it's allowed on a charge
  marked `bill_match_declined` (that flag only stops automatic pairing).
  `can_pair` in the serializer says whether to offer it.
  `flask merge-bill-duplicates` is the one-time cleanup for pairs from
  before this existed: a dry run unless `--apply`, counts per account
  only unless `--details` (those are other people's transactions), the
  same rules plus one more: a charge another tracked bill could claim
  just as well (two $30 bills, one $30 payment) is never merged.
- **A one-off charge doesn't reset a bill** (2026-10-23). A paid
  occurrence keeps what it expected in `pairing["bill"]["amount"]`, and
  `bill_matching.expected_amount(template)` decides what the next
  occurrence expects: the new amount only once it's confirmed (the person
  taps "Expect $X from now on", `pairing["new_price"]`, or two charges in a
  row agree), otherwise the usual amount. Everything that reads a bill's
  coming amount uses it (`generate_due_recurring`, Safe to Spend's bills
  still due, Plan's unposted bills, Coming up, this week's renewals), never
  the latest row's `amount` directly. A charge at the amount the bill was
  last charged still matches it past the usual 10%, so a real price rise
  pairs again the second month. `price_change` in the serializer, web
  `/activity/<id>/new-price`, API `/transactions/<id>/new-price`.
- **A purchase can be split across categories** (`services/splits.py`,
  2026-10-23). Each part is its own Transaction sharing
  `Transaction.split_group`, so budgets, Safe to Spend, charts and the AI
  snapshot count every part under its own category without knowing splits
  exist. Lists show the purchase once (`screens._one_row_per_purchase`, its
  first row standing for the total, "Split: Groceries, Shopping") except a
  list filtered to categories, which shows the matching parts.
  `split_transaction_record` / `unsplit_transaction_record` back the web
  (`/activity/<id>/split`, `/unsplit`) and the API
  (`/transactions/<id>/split`, `/unsplit`); parts must add up to the
  purchase to the cent and each part gets its share of the locked USD
  value. An edit's name, date and type change every part; amounts and
  categories only change in the split. Deleting deletes the whole
  purchase. A bill's own row can't be split, and a part is never paired
  with a bill or matched as a bill's charge.
- **The pace heads-up is opt-in** (2026-10-23): `User.notify_pace_heads_up`
  defaults off (deeper engagement stays opt-in, per the YNAB lesson above),
  and Today offers it once (`today_screen`'s `pace_prompt`; either answer
  sets `User.pace_prompt_answered`, so it never shows again on any device).
  `finance.routes._check_pace_heads_up` runs first inside
  `_check_budget_overage_alerts`, so every save checks both: one push per
  month (`Budget.pace_alerted_month`) when `MonthSnapshot.pace_status` is
  "ahead" from the 5th on, re-armed once spending is back at an even pace.
  The app does the same locally (`Reminders.checkPace`).
- **One design language across web and apps: Daybreak** (written down
  2026-10-22 in the artha-apple repo's `Docs/Design.md`). The web's
  tokens in `static/css/style.css` mirror `Palette.swift` value for value
  (including the three radii, `--radius-card/inner/small`), and the same
  color rules hold: blue is tappable, green is on pace, amber is "look
  here" (over budget, ahead of pace, net worth down, a stale balance),
  and red is only ever a destructive action, never a money status.
- **The website's money pages render the app's own screens** (the
  2026-10 web redesign). `services/screens.py` builds the dicts behind
  Today, Activity (`transactions_screen`), Plan, Month by month and
  Wealth; the API returns them as JSON and the web pages (`index.html`,
  `activity.html`, `plan.html`, `accounts.html`) render the same dicts,
  so the two can't disagree. Money in them is two-decimal strings,
  formatted in templates with the `money` filter
  (`utils.format_money`, matching `currency.js`'s per-currency style:
  BDT lakh grouping, EUR as `1.234,56 €`) instead of the older
  `data-money-value` + client-side formatting. After any change the page
  reloads (`shell.js`'s `toastAfterReload`) rather than patching numbers
  client-side, per the currency trap below. Navigation matches the apps:
  Today, Activity, Plan, Wealth, then More (AI Assistant, Scenarios,
  Reports, which keeps the older `/finance` page's Spending, Income,
  Cash Flow and Recurring charts, Notes, Calendar, Calculator, What's
  new). The Add sheet (`partials/add_sheet.html`, N on the keyboard) is
  on every page. Since 2026-10-22 the desktop has no top bar: search is
  in the sidebar (any `[data-search-open]` element opens it), feedback is
  in the sidebar footer (`[data-feedback-open]` forwards to the hidden
  `#feedback-fab`), and the top bar only renders below `md`. Font sizes
  are rem, never px, so text follows the browser's font-size setting.
  `templates/privacy.html` has a section on the iPhone, iPad and Mac apps
  (what each keeps, widgets, Siri) and one on website notifications; keep it in
  step with what the app does.
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
- Bank statement import (`artha/blueprints/finance/routes.py`) runs
  through two shared functions, `preview_statement` and
  `import_statement_rows`, behind both the website's import sheet and the
  app's `POST /api/v1/imports/preview` (multipart, AI-rate-limited per
  account: `user_or_ip_key` also knows the API's `g.api_user`) and
  `POST /api/v1/imports` (2026-10-23). The parsers take the person's
  currency as `fallback_currency` instead of reading `current_user`, since
  the API has no web session. The app reads a picked or "Open in Artha"
  file in place (security-scoped, never copied) and uploads it from memory.
  Since 2026-10-23 the format rules live in `services/statement_reader.py`
  (amounts like 1.234,56 / 12.00 DR / (12.00), dates in the main European
  languages and without a year, names cleaned of card and ACH clutter,
  OFX/QFX and QIF, the statement's own totals, transfers between the
  person's own accounts). The whole preview runs inside
  `_IMPORT_TIME_BUDGET_SECONDS` (22s): Render's gunicorn stops a request at
  30s with one worker, and a real Bank of America statement that went to AI
  over its "Beginning balance" line used to die there. AI never runs when
  the rows add up to the statement's totals (`check_totals` "match"); when
  it does run, it reads a few pages per call, in parallel, each call capped
  by the time left. Rows already in Artha (exact, or the same amount within
  three days under a name sharing a real word, e.g. an Apple Pay entry) and
  transfers between the person's own accounts start unticked.
  Reading is two-tiered: a fast, free, deterministic regex parser runs first and
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
- Multi-currency (added 2026-09-02): `Transaction.currency` is the
  currency a transaction actually happened in; `Transaction.usd_value`
  is that amount converted to USD **at creation/import time and locked
  forever** — `artha/services/exchange_rate_service.py`'s
  `lock_usd_value()`. USD is purely an internal pivot, never shown to a
  user. Two completely different operations read this data, and mixing
  them up is the bug to avoid:
  - **A single transaction's own display** (an Activity row, the
    Activity editor's amount, a recurring bill's own row) always shows
    the *exact native* `amount`/`currency` — never converted, never the
    display currency's symbol slapped on a foreign number. Server-side
    that's the `money` filter with the transaction's own currency
    (`partials/money_macros.html`'s `tx_amount`); client-side,
    `currency.js`'s `formatMoneyIn()` with that currency, not
    `formatMoney()` (which uses the display currency).
  - **Which currencies exist** (widened 2026-10-22 from a fixed six to
    every one the rate provider covers, 157): `artha/currency_table.py`,
    generated from open.er-api.com's code list plus the browser's own
    Intl data (names and symbols), so `utils.format_money` and
    `currency.js` write each currency the same way. The app has a copy,
    `Shared/Currencies.swift` in artha-apple; regenerate both together.
    Every picker uses `partials/currency_options.html` (the six common
    ones first). The original six keep their old symbols ($ for CAD/AUD).
  - **Any aggregate across more than one transaction** (finance totals,
    budgets, chart data, the AI's financial-context snapshot) sums
    `Transaction.value_in_usd` (Python) / `coalesce(usd_value, amount)`
    (SQL — see `finance_totals()`), then converts that USD sum to the
    user's own `preferred_currency` **server-side**, via
    `exchange_rate_service.convert_usd_to()`, before it ever reaches a
    template or JSON response.
  - **`Budget.monthly_cap`, `CategoryBudget.monthly_cap`, and
    `Scenario`'s `monthly_cost`/`monthly_savings`/`one_time_cost`** each
    have their own `currency` column (added 2026-09-02, hours after
    multi-currency itself shipped, once a real user's $5,000 cap got
    silently misread as a 5,000-unit cap in BDT the moment they switched
    display currency) — captured the same way a manual transaction's
    currency is, from `current_user.preferred_currency` at the moment
    the cap/cost is saved, re-captured on every edit. These have no
    "creation moment" worth locking a USD rate to the way a Transaction
    does, so `exchange_rate_service.convert_amount(amount, from_currency,
    to_currency)` (the two-currency sibling of `convert_usd_to()`)
    converts them **live, at comparison time**, not once and cached.
    Any new currency-less money field on a model works the same way:
    give it its own `currency` column and convert with `convert_amount()`
    before comparing it against anything already in a different
    currency — never assume a bare number is safely comparable against
    a converted one just because both look like plain numbers.
  - The one field that must **never** show a converted number: the
    Activity editor's amount (`#detail-amount`, `static/js/activity.js`)
    is the literal value posted back on save, so if it ever displayed a
    converted figure, editing an unrelated field (the name) and saving
    would silently overwrite the real native amount with a converted
    one. A foreign transaction's converted equivalent goes in a separate,
    non-editable element (`detail-sub`, `tx_amount`'s "about" line).
- Per-user reminder delivery (added 2026-09-02): `flask
  send-renewal-reminders` (`artha/cli.py`) runs hourly now, not once
  daily — it computes each user's own local "now" via
  `utils.user_now(owner)` and only sends if the current hour matches
  their own `User.reminder_hour` (default 8). The external Render Cron
  Job's own schedule has to be `0 * * * *`, set by hand outside this
  repo — a stale once-daily schedule there would silently mean nobody
  except users who picked 8am ever gets notified.

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
  stale. A wrong privacy claim users actually see is worse than no page
  at all.
- **Theme follows the device** (2026-10): `prefers-color-scheme` decides
  light or dark, live, unless the person taps the theme toggle, which
  pins a choice under `localStorage["theme-choice"]` (tapping back to
  the device's own setting clears it). The older `"theme"` key is
  deliberately ignored: the previous theme.js wrote the device setting
  into it on every first visit, so it can't tell a choice from a
  default. The FOUC script in `base.html` and `static/js/theme.js` must
  agree on this.
- **`hide_app_shell` pages split into two families, only one of which
  should ever be theme-aware (fixed 2026-08-31).** Login, Register,
  forgot/reset password, and the 2FA login-verify step are steps in
  *authenticating* — nobody has a session yet, so they keep the fixed-dark
  `auth-bg`/`auth-card`/`auth-input`/`auth-button` classes in
  `static/css/style.css` as a deliberate, consistent gateway identity
  regardless of the visitor's theme preference. Every other
  `hide_app_shell` page (Privacy, Edit Profile, Change Password, 2FA
  enable, recovery codes) is reached by an already-logged-in user with
  their own theme already set, and uses the theme-aware counterparts
  instead: `settings-bg`/`settings-card`/`settings-tile` plus the
  existing `field-input`/`btn-primary`/`text-primary`/`text-secondary`/
  `text-tertiary` classes, never a hardcoded `text-white`/`text-slate-300`
  Tailwind utility or a literal `rgba(...)` background. Privacy & Security
  shipped with the fixed-dark classes and stayed dark even in light mode
  until a real user reported it. A new page in this second family should
  follow the same theme-aware classes from the start, not copy-paste an
  existing auth-* page. The one legitimate exception is a QR code's own
  background box (`enable_2fa.html`) — that stays hardcoded white, since
  the SVG's black modules need guaranteed contrast to scan.
- **Production static assets cache for 5 minutes** (`SEND_FILE_MAX_AGE_DEFAULT`
  in `ProductionConfig`, 2026-08-31) — added after profiling showed every
  navigation re-fetching all ~18 JS/CSS files with a `Cache-Control: no-cache`
  round trip, a real cost on a full-page-reload app where that happens on
  every single tab tap. Werkzeug's own ETag (present regardless of this
  setting) still catches a change on revalidation, so this bounds staleness
  to 5 minutes rather than removing the safety net — deliberately not the
  indefinite/`immutable` caching that caused the real Service Worker
  staleness incident above; this is a much shorter, bounded window on a
  different mechanism. Deliberately not set on `DevelopmentConfig`, so it
  can't compound with "the dev server doesn't hot-reload" below and make a
  stale asset look like a hot-reload failure.

## Known traps

- **A plain inline `<script>` runs the instant the parser reaches it, not
  once the page is "ready"** (found 2026-09-15, adding the recurring-
  transaction interval/end-date picker). `templates/finance.html` has a
  bare, non-deferred `<script>` block partway down the page (the budget-
  panel edit-toggle wiring) that runs synchronously as the HTML parser
  reaches it — any element defined *later* in the same file (the add-
  transaction form, further down) simply doesn't exist in the DOM yet at
  that moment. A `document.querySelector(...)` for one of those elements
  silently returns `null` there — no error, the enclosing `if (el) {...}`
  guard just never runs, so a feature that looks fully wired quietly does
  nothing on page load. Wrap only the new code in
  `document.addEventListener("DOMContentLoaded", function () {...})`
  rather than moving or rewriting the surrounding script — the existing
  code in that block already depends on running immediately (or on
  earlier-in-page elements), and doesn't need the same fix. Caught in
  this same session, before shipping, by a live check that found the
  reveal simply never happened — worth checking for on sight any time new
  JS in this file (or any template with a similar early inline `<script>`)
  references an element that appears later in the same document.
- **Switching currency needs a real page reload, not just a client-side
  reformat** (found 2026-09-02, the same day multi-currency shipped).
  `static/js/currency.js`'s `formatMoney()`/`currency-refresh-ui` only
  reformats numbers already sitting in the DOM — fine for a page that
  never did any conversion in the first place, but finance totals,
  budgets, and Scenario comparisons are now computed **server-side**
  into whichever currency `preferred_currency` is. A client-side-only
  currency switch relabels the *old* currency's already-converted
  number with the *new* currency's symbol instead of actually
  reconverting it, which is indistinguishable from the original
  pre-multi-currency relabeling bug this whole feature was built to
  fix. `static/js/settings.js`'s `persistCurrencyToAccount()` now calls
  `window.location.reload()` once the `/set_currency` save succeeds —
  any other place that changes `preferred_currency` client-side needs
  the same reload, not just a `currency-refresh-ui` dispatch.
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
  Also caught this same session on the mobile bottom tab bar
  (`#mobile-tabbar`, added 2026-08-30): its `<nav>` needs its own
  inline `display:flex` for the row of tabs, so `class="md:hidden"`
  was put directly on that same element - which meant the inline
  style beat Tailwind's `md:hidden` media query exactly like every
  other instance of this trap, and the tab bar stayed visible on
  desktop. Fixed the same way as `settings-btn`: `md:hidden` moved
  onto a plain wrapper `<div>` around the `<nav>`, with no inline
  style of its own to fight. Caught in this same change before it
  shipped, not by a later user report - worth checking for on sight
  any time `class="md:hidden"` (or `hidden md:block`, etc.) lands on
  an element that also carries an inline `display` in its own
  `style="..."`.
- **Tailwind is precompiled, not the CDN script (changed 2026-08-31).**
  `templates/base.html` links `static/css/tailwind.css`, built from
  `static/css/tailwind-src.css` via `npm run build:css` (Tailwind CLI,
  see `package.json`). Adding or changing a Tailwind utility class
  anywhere in a template — `hidden`, `md:block`, a new `grid-cols-*`,
  whatever — does nothing until that build runs again and the
  regenerated `tailwind.css` is committed; there's no dev-server
  watch process wired in yet, and no build step in the deploy pipeline
  either, so the compiled file is a committed static asset like
  `style.css`, not a generated artifact. Forgetting the rebuild is a
  silent failure, not an error: the class just doesn't render, same
  family of bug as every other trap in this section. Run
  `npm run watch:css` while actively touching Tailwind classes to
  avoid this entirely.
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
