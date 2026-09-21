// static/js/search.js (module)
// Global ⌘K search palette — queries /search across notes, transactions,
// scenarios, and events, scoped to the signed-in user. Present on every
// page via base.html; guards on missing elements rather than assuming
// anything beyond the trigger/modal itself exists.

const GROUPS = [
    { key: "pages", label: "Pages" },
    { key: "notes", label: "Notes" },
    { key: "transactions", label: "Transactions" },
    { key: "scenarios", label: "Scenarios" },
    { key: "events", label: "Events" },
];

// Fixed, client-known navigation destinations — every top-level page plus
// Finance's own tabs (which finance.html reads back from ?tab=, see
// fpActivateTab's auto-activation below and active_tab in
// finance/routes.py). Matched entirely client-side against title+keywords
// with no debounce, so "Pages" results appear instantly on every
// keystroke rather than waiting on the /search round trip the other
// groups need. The Admin entry is spliced in separately (see
// buildPagesList) since whether it belongs depends on the signed-in user.
const BASE_PAGES = [
    { title: "Dashboard", keywords: "home", url: "/" },
    { title: "Finance", keywords: "money", url: "/finance" },
    { title: "Finance · Overview", keywords: "savings rate budget trend biggest category", url: "/finance?tab=overview" },
    { title: "Finance · Transactions", keywords: "list add transaction search filter", url: "/finance?tab=transactions" },
    { title: "Finance · Spending", keywords: "breakdown category expenses", url: "/finance?tab=spending" },
    { title: "Finance · Income", keywords: "earnings", url: "/finance?tab=income" },
    { title: "Finance · Cash Flow", keywords: "net income expense trend", url: "/finance?tab=cashflow" },
    { title: "Finance · Recurring", keywords: "bills subscriptions renewals", url: "/finance?tab=recurring" },
    { title: "AI Assistant", keywords: "chat ask artha", url: "/ai" },
    { title: "Scenarios", keywords: "what if plan projection", url: "/scenarios/" },
    { title: "Notes", keywords: "", url: "/notes" },
    { title: "Calendar", keywords: "events schedule", url: "/calendar" },
    { title: "Calculator", keywords: "math", url: "/calculator" },
];

function initGlobalSearch() {
    const trigger = document.getElementById("global-search-trigger");
    // The desktop trigger sits inline in the top bar's centered search box,
    // which is hidden below the sm: breakpoint to make room there — so
    // mobile gets its own icon-only trigger next to the hamburger instead
    // of losing search entirely below 640px.
    const mobileTrigger = document.getElementById("global-search-trigger-mobile");
    const backdrop = document.getElementById("global-search-backdrop");
    const input = document.getElementById("global-search-input");
    const resultsEl = document.getElementById("global-search-results");
    if (!trigger || !backdrop || !input || !resultsEl) return;

    const pages = backdrop.dataset.isAdmin === "true"
        ? [...BASE_PAGES, { title: "Admin", keywords: "feedback users changelog", url: "/admin" }]
        : BASE_PAGES;

    const EMPTY_HTML = '<p class="global-search-empty">Type to search your notes, transactions, scenarios, events, or jump to a page.</p>';
    const NO_RESULTS_HTML = '<p class="global-search-no-results">No results.</p>';

    let debounceTimer = null;
    let activeIndex = -1;
    let resultLinks = [];
    let currentRequestId = 0;
    // The DB-backed groups (notes/transactions/scenarios/events) arrive
    // async and debounced; Pages is recomputed synchronously on every
    // keystroke instead, so this holds the last known server data to
    // merge alongside whatever Pages just matched, rather than the two
    // racing each other and one clobbering the other's render.
    let lastServerData = { notes: [], transactions: [], scenarios: [], events: [] };

    function matchPages(query) {
        const q = query.toLowerCase();
        return pages.filter((p) => (p.title + " " + p.keywords).toLowerCase().includes(q));
    }

    function refreshIcons() {
        if (window.lucide) window.lucide.createIcons();
    }

    function setActiveIndex(index) {
        resultLinks.forEach((el) => el.classList.remove("active"));
        if (index >= 0 && index < resultLinks.length) {
            activeIndex = index;
            resultLinks[activeIndex].classList.add("active");
            resultLinks[activeIndex].scrollIntoView({ block: "nearest" });
        } else {
            activeIndex = -1;
        }
    }

    function renderResults(data) {
        resultsEl.innerHTML = "";
        resultLinks = [];
        activeIndex = -1;

        const anyResults = GROUPS.some((g) => (data[g.key] || []).length > 0);
        if (!anyResults) {
            resultsEl.innerHTML = NO_RESULTS_HTML;
            return;
        }

        GROUPS.forEach((group) => {
            const items = data[group.key] || [];
            if (!items.length) return;

            const label = document.createElement("div");
            label.className = "global-search-group-label";
            label.textContent = group.label;
            resultsEl.appendChild(label);

            items.forEach((item) => {
                const link = document.createElement("a");
                link.className = "global-search-result";
                link.href = item.url;

                const title = document.createElement("span");
                title.className = "global-search-result-title";
                title.textContent = item.title;
                link.appendChild(title);

                if (item.snippet) {
                    const snippet = document.createElement("span");
                    snippet.className = "global-search-result-snippet";
                    snippet.textContent = item.snippet;
                    link.appendChild(snippet);
                }

                resultsEl.appendChild(link);
                resultLinks.push(link);
            });
        });

        refreshIcons();
    }

    function runSearch(query) {
        const requestId = ++currentRequestId;
        fetch("/search?q=" + encodeURIComponent(query), {
            credentials: "same-origin",
            headers: { "X-Requested-With": "XMLHttpRequest" },
        })
            .then((res) => (res.ok ? res.json() : Promise.reject(res)))
            .then((data) => {
                // A slower earlier request resolving after a faster later
                // one would otherwise overwrite the results for what the
                // user is currently typing with stale ones for what they
                // typed a moment ago.
                if (requestId !== currentRequestId) return;
                lastServerData = data;
                renderResults({ pages: matchPages(query), ...data });
            })
            .catch(() => {
                if (requestId !== currentRequestId) return;
                resultsEl.innerHTML = '<p class="global-search-no-results">Search failed. Try again.</p>';
            });
    }

    function onInput() {
        const query = input.value.trim();
        clearTimeout(debounceTimer);

        if (query.length < 2) {
            currentRequestId++; // invalidate any in-flight request
            resultsEl.innerHTML = query.length === 0 ? EMPTY_HTML : NO_RESULTS_HTML;
            resultLinks = [];
            activeIndex = -1;
            return;
        }

        // Pages need no network round trip, so they render on every
        // keystroke rather than waiting on the same 200ms debounce the
        // DB-backed groups below still use -- merged with whichever
        // server data is still on hand from the last resolved fetch,
        // same "stays put until the new one lands" staleness the
        // debounce already implied before Pages existed.
        renderResults({ pages: matchPages(query), ...lastServerData });

        debounceTimer = setTimeout(() => runSearch(query), 200);
    }

    function openSearch() {
        backdrop.hidden = false;
        input.value = "";
        lastServerData = { notes: [], transactions: [], scenarios: [], events: [] };
        resultsEl.innerHTML = EMPTY_HTML;
        resultLinks = [];
        activeIndex = -1;
        refreshIcons();
        // Focus needs to happen after the element is actually visible.
        requestAnimationFrame(() => input.focus());
    }

    function closeSearch() {
        if (backdrop.hidden) return;
        backdrop.hidden = true;
        clearTimeout(debounceTimer);
        currentRequestId++;
        // Whichever trigger is actually visible at the current viewport
        // width gets focus back — the other one is display:none and a
        // focus() call on it is a silent no-op, so this can't double-focus.
        (mobileTrigger && mobileTrigger.offsetParent ? mobileTrigger : trigger).focus();
    }

    trigger.addEventListener("click", openSearch);
    if (mobileTrigger) mobileTrigger.addEventListener("click", openSearch);

    backdrop.addEventListener("click", (e) => {
        if (e.target === backdrop) closeSearch();
    });

    input.addEventListener("input", onInput);

    input.addEventListener("keydown", (e) => {
        if (e.key === "ArrowDown") {
            e.preventDefault();
            if (resultLinks.length) setActiveIndex(Math.min(activeIndex + 1, resultLinks.length - 1));
        } else if (e.key === "ArrowUp") {
            e.preventDefault();
            if (resultLinks.length) setActiveIndex(Math.max(activeIndex - 1, 0));
        } else if (e.key === "Enter") {
            if (activeIndex >= 0 && resultLinks[activeIndex]) {
                e.preventDefault();
                resultLinks[activeIndex].click();
            } else if (resultLinks.length) {
                e.preventDefault();
                resultLinks[0].click();
            }
        }
    });

    document.addEventListener("keydown", (e) => {
        const key = (e.key || "").toLowerCase();
        if ((e.metaKey || e.ctrlKey) && key === "k") {
            e.preventDefault();
            if (backdrop.hidden) openSearch();
            else closeSearch();
            return;
        }
        if (e.key === "Escape" && !backdrop.hidden) closeSearch();
    });
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initGlobalSearch);
} else {
    initGlobalSearch();
}
