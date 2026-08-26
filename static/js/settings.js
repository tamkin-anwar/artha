// static/js/settings.js (module)

import { setCurrencyCode, getCurrencyCode, hasStoredPreference, applyCurrencyToAmountPrefixes } from "./currency.js";

function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
}

// Best-effort: this device's own localStorage already has the change
// either way, so a failed request here just means a *different* device
// won't see it until the next successful save, not that anything on this
// one is wrong.
function persistCurrencyToAccount(code) {
    fetch("/set_currency", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        body: JSON.stringify({ code }),
    }).catch(() => {});
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
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initCurrency);
} else {
    initCurrency();
}