// static/js/search.js (module)
// Global ⌘K search palette — queries /search across notes, transactions,
// scenarios, and events, scoped to the signed-in user. Present on every
// page via base.html; guards on missing elements rather than assuming
// anything beyond the trigger/modal itself exists.

const GROUPS = [
    { key: "notes", label: "Notes" },
    { key: "transactions", label: "Transactions" },
    { key: "scenarios", label: "Scenarios" },
    { key: "events", label: "Events" },
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

    const EMPTY_HTML = '<p class="global-search-empty">Type to search your notes, transactions, scenarios, and events.</p>';
    const NO_RESULTS_HTML = '<p class="global-search-no-results">No results.</p>';

    let debounceTimer = null;
    let activeIndex = -1;
    let resultLinks = [];
    let currentRequestId = 0;

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
                renderResults(data);
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

        debounceTimer = setTimeout(() => runSearch(query), 200);
    }

    function openSearch() {
        backdrop.hidden = false;
        input.value = "";
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
