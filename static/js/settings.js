// static/js/settings.js (module)

import { setCurrencyCode, getCurrencyCode, applyCurrencyToAmountPrefixes } from "./currency.js";

function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
}

// The account is the source of truth for the display currency (see
// accountCurrencyCode() in currency.js), so a change only takes effect
// once the server has saved it -- then the page reloads on purpose.
// Dashboard/Finance totals, budgets, and comparisons are converted
// server-side into whichever currency the account is set to (see
// CLAUDE.md's Multi-currency note); a client-side relabel can only
// reformat the *symbol* on numbers already baked into the page, so
// nothing is relabeled before the reload either -- doing so used to
// flash the previous currency's converted figures under the new
// currency's symbol, the same bug a user hit live. A failed save puts
// the selects back on the account's currency instead of leaving them
// claiming a currency the page isn't actually showing.
function persistCurrencyToAccount(code) {
    const revert = () => syncCurrencySelects(getCurrencyCode());
    fetch("/set_currency", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        body: JSON.stringify({ code }),
    })
        .then((res) => {
            if (!res.ok) return revert();
            setCurrencyCode(code);
            window.location.reload();
        })
        .catch(revert);
}

function setSelectValueIfPresent(selectEl, value) {
    if (!selectEl) return;
    const optionExists = Array.from(selectEl.options).some((opt) => opt.value === value);
    if (optionExists) selectEl.value = value;
}

function syncCurrencySelects(code) {
    setSelectValueIfPresent(document.getElementById("currency-select"), code);
}

function applyCurrencyEverywhere() {
    applyCurrencyToAmountPrefixes();
    document.dispatchEvent(new CustomEvent("currency-refresh-ui"));
}

function bindCurrencySelect(selectEl) {
    if (!selectEl) return;

    if (selectEl.dataset.bound === "1") return;
    selectEl.dataset.bound = "1";

    selectEl.addEventListener("change", () => {
        const code = selectEl.value;
        syncCurrencySelects(code);
        persistCurrencyToAccount(code);
    });
}

// Detected fact about where the browser is, not a user preference — so
// unlike currency there's nothing to ask the user or show a control for.
// Runs once per page load; the server only writes on an actual change
// (set_timezone), so a device that already matches costs one cheap no-op
// request, not a write every time.
function syncTimezoneToAccount() {
    let detected;
    try {
        detected = Intl.DateTimeFormat().resolvedOptions().timeZone;
    } catch {
        return;
    }
    if (!detected) return;

    const known = document.body.dataset.userTimezone || "";
    if (detected === known) return;

    fetch("/set_timezone", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        body: JSON.stringify({ timezone: detected }),
    })
        .then(() => { document.body.dataset.userTimezone = detected; })
        .catch(() => {});
}

function initCurrency() {
    // getCurrencyCode() already returns the signed-in account's currency
    // (currency.js reconciled this browser's stored value at import time),
    // so there's nothing to adopt or choose between here any more.
    const saved = getCurrencyCode();

    syncCurrencySelects(saved);

    bindCurrencySelect(document.getElementById("currency-select"));

    applyCurrencyEverywhere();

    // On every signed-in page (base.html stamps data-display-currency on
    // <body> only for one), not just where a currency control happens to
    // be: the person's own "today" depends on it.
    if (document.body.dataset.displayCurrency) syncTimezoneToAccount();
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initCurrency);
} else {
    initCurrency();
}