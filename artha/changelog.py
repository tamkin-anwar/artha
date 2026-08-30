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

Backfilled 2026-08-29 with the highlights of everything shipped since
the first tracked commit (2026-01-04), not just entries written live
going forward. Curated, not exhaustive by design — this is a highlight
reel of what a user would actually notice, not a mirror of git log.
Closely-related commits from the same push are grouped under one entry
rather than listed one-for-one.

Fields: date (YYYY-MM-DD, the day it shipped), category ("new" |
"improved" | "fixed"), title, body. Newest first -- this list is the
render order too.
"""

CHANGELOG_ENTRIES = [
    {
        "date": "2026-08-29",
        "category": "fixed",
        "title": "The top bar and slide-out menu, tightened up on mobile",
        "body": "The search icon in the top bar was landing in the wrong spot on phones, and scrolling the slide-out menu used to drag the whole dashboard behind it along too. Both fixed.",
    },
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
    {
        "date": "2026-08-26",
        "category": "new",
        "title": "Set a budget per category, not just overall",
        "body": "Give Dining, Groceries, or any category its own spending cap, on top of your overall monthly budget, so you know exactly where the overspending is coming from.",
    },
    {
        "date": "2026-08-26",
        "category": "improved",
        "title": "The calculator handles loans and compound interest",
        "body": "Ask it to work out a loan payment or how interest compounds over time, right alongside your everyday arithmetic.",
    },
    {
        "date": "2026-08-26",
        "category": "new",
        "title": "Ask the AI Assistant to add things for you",
        "body": "Describe a transaction, a note, a calendar event, a budget, or a recurring bill in plain language, and it drafts the entry for you to review. Nothing saves until you approve it.",
    },
    {
        "date": "2026-08-25",
        "category": "new",
        "title": "Forgot your password? Reset it yourself",
        "body": "A self-serve, email-based reset, no need to ask anyone for help getting back into your account.",
    },
    {
        "date": "2026-08-23",
        "category": "new",
        "title": "Import a bank statement, CSV or PDF",
        "body": "Upload a statement, even a scanned or password-protected PDF, and Artha reads it, sorts transactions into categories, and drops them straight into Finance.",
    },
    {
        "date": "2026-08-23",
        "category": "new",
        "title": "See spending, income, and cash flow over time",
        "body": "New tabs on the Finance page break your money down by category and compare each month to the same month last year, not just to the month before it.",
    },
    {
        "date": "2026-08-21",
        "category": "new",
        "title": "Recurring events on the Calendar",
        "body": "Set an event to repeat weekly or monthly instead of adding it by hand every time.",
    },
    {
        "date": "2026-08-20",
        "category": "new",
        "title": "Search everything from anywhere in the app",
        "body": "Press the search bar (or ⌘K) to jump straight to a transaction, note, event, or scenario without hunting through pages.",
    },
    {
        "date": "2026-08-15",
        "category": "new",
        "title": "Notes get a 30-day Trash",
        "body": "Delete a note by mistake and it waits in Trash for 30 days before it's gone for good, the same safety net your transactions already have.",
    },
    {
        "date": "2026-08-14",
        "category": "new",
        "title": "Archive notes instead of deleting them",
        "body": "Done with a note but not ready to lose it? Archive it. It's out of your way without being gone.",
    },
    {
        "date": "2026-08-14",
        "category": "improved",
        "title": "Tag notes however makes sense to you",
        "body": "Tags are no longer a fixed list. Type your own and Artha remembers it for next time.",
    },
    {
        "date": "2026-08-03",
        "category": "new",
        "title": "Never miss a bill, note, or event",
        "body": "Turn on push notifications and Artha nudges you the day something's due, right from your phone or browser.",
    },
    {
        "date": "2026-08-02",
        "category": "new",
        "title": "Set a monthly budget",
        "body": "Give yourself an overall spending cap and watch how close you're tracking to it as the month goes.",
    },
    {
        "date": "2026-08-02",
        "category": "new",
        "title": "Send feedback without leaving the app",
        "body": "Found a bug or have an idea? There's a feedback button right in the app now, straight to the person building it.",
    },
    {
        "date": "2026-08-01",
        "category": "improved",
        "title": "The dashboard became an actual daily briefing",
        "body": "Rebuilt to open with what actually matters today, not a wall of static widgets.",
    },
    {
        "date": "2026-07-31",
        "category": "new",
        "title": "The calculator converts units and currency",
        "body": "Type \"50 miles in km\" or \"100 USD in GBP\" and get a real answer, right inside the same calculator you use for arithmetic.",
    },
    {
        "date": "2026-07-31",
        "category": "new",
        "title": "Block out time on the Calendar",
        "body": "Drag out a block on your day to reserve time for something, not just drop in a single point-in-time event.",
    },
    {
        "date": "2026-07-30",
        "category": "new",
        "title": "Notes: pin, color-code, tag, and set due dates",
        "body": "A real toolkit for organizing notes instead of a plain list, plus checklists for anything with steps.",
    },
    {
        "date": "2026-07-08",
        "category": "new",
        "title": "Rich text in Notes",
        "body": "Bold, headings, bulleted and numbered lists, checklists: real formatting instead of plain text.",
    },
    {
        "date": "2026-07-07",
        "category": "new",
        "title": "Finance, Calendar, Calculator, and Notes each got their own page",
        "body": "What used to be small dashboard widgets became full, purpose-built pages: monthly tabs and a trend chart for Finance, a proper month-grid Calendar, a natural-language Calculator, a Notes page you can actually browse. Recurring transactions arrived too, so a bill you pay every month only needs to be entered once.",
    },
    {
        "date": "2026-07-06",
        "category": "new",
        "title": "Meet Scenarios: model a decision before you make it",
        "body": "Ask what happens if you took a pay cut, or moved to a cheaper apartment, and see the real effect on your finances before you decide anything.",
    },
    {
        "date": "2026-07-06",
        "category": "improved",
        "title": "A full visual redesign",
        "body": "A new sidebar, glass-panel cards, and a more considered typeface throughout. Same Artha, sharper edges.",
    },
    {
        "date": "2026-06-23",
        "category": "new",
        "title": "Say hello to the Artha AI Assistant",
        "body": "Ask questions about your own spending and get an answer grounded in your real data, right from the dashboard.",
    },
    {
        "date": "2026-06-22",
        "category": "fixed",
        "title": "Your balances are now exact, to the cent",
        "body": "Money is now stored and calculated with exact precision throughout, closing the door on the kind of rounding drift that could quietly throw a balance off by a cent or two over time.",
    },
    {
        "date": "2026-04-21",
        "category": "new",
        "title": "Change your password, and see your real name in the app",
        "body": "A proper change-password flow, plus your first and last name now show up throughout Artha instead of just a username.",
    },
    {
        "date": "2026-02-14",
        "category": "new",
        "title": "Pick your currency",
        "body": "USD, GBP, BDT, EUR, CAD, or AUD: set it once in Settings and it's used everywhere in Finance.",
    },
    {
        "date": "2026-01-24",
        "category": "improved",
        "title": "A dashboard you can rearrange",
        "body": "Drag your dashboard cards into whatever order actually makes sense to you.",
    },
    {
        "date": "2026-01-10",
        "category": "new",
        "title": "Drag to reorder your transactions and notes",
        "body": "Put them in the order you think in, not just the order you happened to add them.",
    },
    {
        "date": "2026-01-04",
        "category": "new",
        "title": "Artha's first release",
        "body": "The original dashboard: transactions, notes, and one place to see it all together, plus offline support so the app still opens without a connection.",
    },
]
