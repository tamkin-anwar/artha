// static/js/settings.js (module)

import { setCurrencyCode, getCurrencyCode, hasStoredPreference, applyCurrencyToAmountPrefixes } from "./currency.js";

function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
}

// The account-wide save itself is still best-effort (this device's own
// localStorage already has the change either way, so a failed request
// here just means a *different* device won't see it until the next
// successful save) -- but a successful save reloads the page on purpose.
// Dashboard/Finance totals, budgets, and comparisons are converted
// server-side into whichever currency the account is set to (see
// CLAUDE.md's Multi-currency note); the client-side relabel this
// triggers before the reload (applyCurrencyEverywhere, still called by
// the caller) only reformats the *symbol* on numbers already baked into
// the page at load time, so without this reload those totals stay
// showing the previous currency's converted figures under a new
// currency's symbol until the next full page load happens to occur —
// a real bug a user hit live, not just a cosmetic lag.
function persistCurrencyToAccount(code) {
    fetch("/set_currency", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        body: JSON.stringify({ code }),
    })
        .then((res) => { if (res.ok) window.location.reload(); })
        .catch(() => {});
}

function setSelectValueIfPresent(selectEl, value) {
    if (!selectEl) return;
    const optionExists = Array.from(selectEl.options).some((opt) => opt.value === value);
    if (optionExists) selectEl.value = value;
}

function syncCurrencySelects(code) {
    setSelectValueIfPresent(document.getElementById("currency-select"), code);
    setSelectValueIfPresent(document.getElementById("currency-select-mobile"), code);
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
        const saved = setCurrencyCode(code);

        syncCurrencySelects(saved);
        applyCurrencyEverywhere();
        persistCurrencyToAccount(saved);
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
    // A brand-new device (nothing saved in this browser yet) inherits
    // whatever the account last saved, instead of defaulting to USD — an
    // already-configured device is left exactly as it is. data-user-currency
    // is the same value on both selects (base.html renders it from
    // current_user.preferred_currency), so either one is fine to read.
    if (!hasStoredPreference()) {
        const accountCurrency =
            document.getElementById("currency-select")?.dataset.userCurrency ||
            document.getElementById("currency-select-mobile")?.dataset.userCurrency;
        if (accountCurrency) setCurrencyCode(accountCurrency);
    }

    const saved = getCurrencyCode();

    syncCurrencySelects(saved);

    bindCurrencySelect(document.getElementById("currency-select"));
    bindCurrencySelect(document.getElementById("currency-select-mobile"));

    applyCurrencyEverywhere();

    // Only present in the settings dropdown for a logged-in user — piggybacks
    // on that as the "are we authenticated" check, same as everything else
    // in this function already implicitly does.
    if (document.getElementById("currency-select")) syncTimezoneToAccount();
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initCurrency);
} else {
    initCurrency();
}