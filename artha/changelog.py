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
        "date": "2026-10-23",
        "category": "improved",
        "title": "Artha remembers your own accounts",
        "body": "Leave a transfer to your savings out of an import once, and next time money moving to or from that account starts unticked, so it never counts as spending. See and forget them under Your accounts in Settings.",
    },
    {
        "date": "2026-10-23",
        "category": "new",
        "title": "Import a paper statement",
        "body": "Scan a paper statement with your iPhone's camera, or upload a photo or a scanned PDF, and Artha reads the transactions off the page. It checks them against the statement's own balances, and you review every row before anything is saved.",
    },
    {
        "date": "2026-10-23",
        "category": "improved",
        "title": "A one-off charge doesn't change your bill",
        "body": "If a bill is charged a different amount once, like a late fee, Artha still expects the usual amount next time. When it's really a new price, tap Expect it from now on, or Artha picks it up after two charges in a row.",
    },
    {
        "date": "2026-10-23",
        "category": "new",
        "title": "Split a purchase across categories",
        "body": "A Costco run that's part groceries, part household can count as both. Open it in Activity, choose Split across categories, and give each part its category and amount. Your budgets count each part where it belongs, and Activity still shows one purchase.",
    },
    {
        "date": "2026-10-23",
        "category": "improved",
        "title": "Statements from any bank, checked to the cent",
        "body": "Importing a statement is faster and reads banks around the world, including OFX, QFX and QIF files. Artha checks the rows against your statement's own totals, cleans up names, and leaves moves between your own accounts unticked.",
    },
    {
        "date": "2026-10-23",
        "category": "new",
        "title": "Import a statement from the app",
        "body": "The iPhone, iPad and Mac apps can now import a bank statement, the same way the website does. Pick a PDF or CSV, or open one in Artha from Mail or Files, check the rows, and tap Import. Anything already in Artha starts unticked.",
    },
    {
        "date": "2026-10-23",
        "category": "new",
        "title": "A heads-up before you go over",
        "body": "Turn on Spending ahead of pace in Settings and Artha tells you partway through the month when you're running ahead, with what a day keeps you on plan. It's off unless you want it.",
    },
    {
        "date": "2026-10-23",
        "category": "new",
        "title": "Pair a payment with its bill yourself",
        "body": "If Artha didn't notice that a payment was for one of your bills, open it in Activity and choose Pair with a bill. Pick the bill from the list and they count once. Unpair works on it too.",
    },
    {
        "date": "2026-10-23",
        "category": "new",
        "title": "Unpair a bill and a charge",
        "body": "If Artha ever pairs a charge with the wrong bill, open it in Activity and tap Unpair. The bill goes back to how it was planned, the charge gets its own name back, and Artha won't pair that charge again.",
    },
    {
        "date": "2026-10-22",
        "category": "fixed",
        "title": "A bill and its payment count once",
        "body": "When a bill you track also shows up as a real charge, from Apple Pay, a bank statement or something you typed in, Artha now sees they're the same payment. Your spending and Safe to spend stay right, and a new price for the bill carries into next month.",
    },
    {
        "date": "2026-10-22",
        "category": "new",
        "title": "Any currency you use",
        "body": "Artha now works in about 160 currencies, from the Hong Kong dollar to the Japanese yen, not just six. Pick yours in Settings and it follows you to every device, the iPhone app included.",
    },
    {
        "date": "2026-10-22",
        "category": "new",
        "title": "Savings goals",
        "body": "Saving for a trip, a laptop or a rainy day? Add a goal in Plan, and Artha shows how much to set aside each month to get there by your date.",
    },
    {
        "date": "2026-10-22",
        "category": "new",
        "title": "Artha spots bills you forgot to track",
        "body": "Charges that repeat like clockwork, like a streaming service or a gym, show up in Plan as \"Looks like a bill\". Track one with a tap so Safe to spend counts it before it hits, or tell Artha it isn't a bill.",
    },
    {
        "date": "2026-10-22",
        "category": "new",
        "title": "Carry over what's left in a category",
        "body": "Turn on carry over for a category limit, and what you didn't spend last month moves into this one. Going over takes it off instead.",
    },
    {
        "date": "2026-10-22",
        "category": "improved",
        "title": "Settings in one place, and a simpler Reports",
        "body": "Currency, appearance, reminders and your calendar link now live on one Settings page. Reports keeps its charts, statement import moved to Activity, and Activity can now set when a repeating bill ends.",
    },
    {
        "date": "2026-10-21",
        "category": "new",
        "title": "A new look, built around four places",
        "body": "Today shows what's safe to spend and how the month is going. Activity lists every transaction by day, with everything editable in place. Plan splits your budget into bills, everyday spending and what's left. Wealth tracks your net worth. Reports, Notes, Calendar and the rest are under More.",
    },
    {
        "date": "2026-10-21",
        "category": "new",
        "title": "Add from anywhere",
        "body": "Press N, or tap the + button, to add a purchase or income from any page. Pick a category or let Artha choose, and set it to repeat if it's a bill.",
    },
    {
        "date": "2026-10-21",
        "category": "improved",
        "title": "Light or dark, the way your device is set",
        "body": "Artha now follows your device's light or dark setting, switching on its own at sunset if your device does. The theme toggle in Settings still pins one if you prefer.",
    },
    {
        "date": "2026-10-04",
        "category": "fixed",
        "title": "Repeating bills now show up even if you didn't open Finance that day",
        "body": "Weekly and every-two-weeks bills only used to appear if Finance happened to be open on the exact day they were due, and a monthly bill could be skipped for a month you never opened it. Every bill that's come due now lands on its own date, keeps the category you gave it, and appears on the dashboard too.",
    },
    {
        "date": "2026-10-04",
        "category": "fixed",
        "title": "Safe to Spend counts every bill left this month",
        "body": "A weekly bill still due three more times this month used to be counted once. Safe to Spend now subtracts every remaining one, and never counts a bill twice.",
    },
    {
        "date": "2026-10-04",
        "category": "fixed",
        "title": "Your month now ends at your own midnight",
        "body": "Near the end of each month, the dashboard and Finance could switch to the next month hours early, or late, depending on your timezone. They now flip over at your own midnight.",
    },
    {
        "date": "2026-10-04",
        "category": "fixed",
        "title": "Money shows in your currency on every page",
        "body": "Accounts, Calendar, and Scenarios could show a dollar sign next to amounts in another currency. They now use your chosen currency, and anything paid in a different one shows exactly what it cost in that currency.",
    },
    {
        "date": "2026-09-22",
        "category": "new",
        "title": "See your real net worth, not just this month's spending",
        "body": "Add your accounts, checking, savings, credit cards, loans, even an estimated value for a house or car, and Artha tracks your total net worth over time on a new Accounts page and right on the dashboard.",
    },
    {
        "date": "2026-09-22",
        "category": "improved",
        "title": "Small animation and empty-state polish across the app",
        "body": "Modals now close with the same smooth motion they open with, and an empty Scenarios list finally has real guidance instead of a bare sentence, part of a wider consistency pass across Notes, Scenarios, and Calendar.",
    },
    {
        "date": "2026-09-22",
        "category": "new",
        "title": "See what's safe to spend, right on your dashboard",
        "body": "A new Safe to Spend figure factors in your budget, what you've spent so far, and any bills still coming due this month, so you know what's actually left to spend, not just what's left in the cap.",
    },
    {
        "date": "2026-09-22",
        "category": "improved",
        "title": "Categorizing a transaction now sticks for that merchant",
        "body": "Fix a transaction's category once, and Artha remembers it for that merchant going forward, the next time it sees the same one it'll use your correction instead of guessing again.",
    },
    {
        "date": "2026-09-20",
        "category": "new",
        "title": "⌘K now takes you anywhere in Artha, not just to search results",
        "body": "Press ⌘K and type a page name, like \"spending\" or \"recurring,\" and jump straight there, alongside your usual search results.",
    },
    {
        "date": "2026-09-20",
        "category": "improved",
        "title": "Adding a note or event on the dashboard feels instant",
        "body": "Adding a quick note used to reload the whole page. Notes and events now save in place with the same instant feedback transactions already had.",
    },
    {
        "date": "2026-09-20",
        "category": "new",
        "title": "Transactions sort themselves into categories",
        "body": "New transactions, recurring bills included, get a category the moment you add them. Already have a backlog of uncategorized ones? One click on Finance sorts them all with AI.",
    },
    {
        "date": "2026-09-20",
        "category": "new",
        "title": "Money, reorganized: Overview and Transactions are now separate",
        "body": "The Transactions tab was getting crowded with savings rate, budget, and trend charts on top of the actual list. Those moved to a new Overview tab, so Transactions is just the list, plus a search box and category filter right above it to narrow things down instantly.",
    },
    {
        "date": "2026-09-15",
        "category": "new",
        "title": "Get a push the moment you go over budget",
        "body": "Budget alerts used to only show up next time you opened Finance. Turn on \"Over-budget alerts\" in Settings and you'll get a push the instant a transaction pushes your overall budget or a category over its cap.",
    },
    {
        "date": "2026-09-15",
        "category": "new",
        "title": "Log a transaction in a different currency, without switching your whole display",
        "body": "The add-transaction form now has its own currency picker, right next to the amount. Log a purchase in whatever it was actually paid in, without changing the currency everything else shows in.",
    },
    {
        "date": "2026-09-15",
        "category": "new",
        "title": "Recurring transactions can now have an end date, and repeat weekly",
        "body": "A recurring transaction used to only ever repeat monthly, forever. Now you can set it to repeat weekly or every two weeks instead, and give it an end date so it stops on its own once it's done.",
    },
    {
        "date": "2026-09-15",
        "category": "new",
        "title": "Export your notes",
        "body": "A new \"Export CSV\" link on the Notes page downloads everything, active and archived alike, as a spreadsheet you can keep as a backup.",
    },
    {
        "date": "2026-09-02",
        "category": "new",
        "title": "Subscribe to your due dates and events in Apple or Google Calendar",
        "body": "A private calendar link, found under Calendar sync in Settings, that any calendar app can subscribe to. Notes, recurring bills, and events all show up there, with their own reminders.",
    },
    {
        "date": "2026-09-02",
        "category": "fixed",
        "title": "Switching currency now actually converts your numbers",
        "body": "Changing the currency setting used to just swap the symbol on the same number. Each transaction now remembers what it was actually in, and totals convert for real when you switch currencies.",
    },
    {
        "date": "2026-09-02",
        "category": "fixed",
        "title": "Budget alerts, fixed after the currency update",
        "body": "Your monthly budget cap could get misread after switching currency, showing a false \"over budget\" warning. It now converts correctly, and switching currency updates your numbers right away instead of needing a refresh.",
    },
    {
        "date": "2026-09-02",
        "category": "new",
        "title": "Pick your own time for daily reminders",
        "body": "Due-today push reminders fired at one fixed hour for everyone. Pick your own time in Settings, and it lands in your own timezone.",
    },
    {
        "date": "2026-08-31",
        "category": "fixed",
        "title": "Privacy & Security, and account settings, now follow your theme",
        "body": "Privacy & Security stayed dark even in light mode, along with Edit Profile, Change Password, and the two-factor setup pages. All of them now switch with the rest of the app.",
    },
    {
        "date": "2026-08-30",
        "category": "new",
        "title": "A real bottom tab bar on mobile",
        "body": "Dashboard, Finance, Notes, and Calendar now sit in a bottom tab bar, the same pattern real iOS apps use, instead of a hamburger menu. Everything else, like AI Assistant, Scenarios, and Calculator, is one tap away under More.",
    },
    {
        "date": "2026-08-30",
        "category": "improved",
        "title": "Mobile navigation, polished",
        "body": "The Artha logo shows up right in the top bar now, not just after opening the menu. The slide-out menu has a real close button and fades in instead of snapping open, and no longer leaves the tab bar peeking out underneath it. Save confirmations don't block the tab bar anymore either.",
    },
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
