// static/js/currency.js

const STORAGE_KEY = "artha_currency";

const FALLBACK = {
    code: "USD",
    locale: "en-US",
};

const CURRENCY_PRESETS = {
    USD: { code: "USD", locale: "en-US", symbol: "$" },
    GBP: { code: "GBP", locale: "en-GB", symbol: "£" },
    EUR: { code: "EUR", locale: "de-DE", symbol: "€" },

    // English digits with lakh crore grouping
    // Using en-IN grouping gives 10,00,00,000 behavior
    BDT: { code: "BDT", locale: "en-IN", symbol: "৳" },

    CAD: { code: "CAD", locale: "en-CA", symbol: "$" },
    AUD: { code: "AUD", locale: "en-AU", symbol: "$" },
};

function safeGetPreset(code) {
    return CURRENCY_PRESETS[code] || CURRENCY_PRESETS[FALLBACK.code];
}

export function getCurrencyCode() {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (!stored) return FALLBACK.code;
    return safeGetPreset(stored).code;
}

// Whether *this* browser has ever explicitly saved a currency — settings.js
// uses this to decide whether to seed a brand-new device from the account's
// saved preference, versus leaving an already-configured device alone.
export function hasStoredPreference() {
    return localStorage.getItem(STORAGE_KEY) !== null;
}

export function getCurrencyPreset() {
    return safeGetPreset(getCurrencyCode());
}

export function setCurrencyCode(code) {
    const preset = safeGetPreset(code);
    localStorage.setItem(STORAGE_KEY, preset.code);
    document.dispatchEvent(new CustomEvent("currency-changed", { detail: { currency: preset.code } }));
    return preset.code;
}

function _format(preset, value, { wholeUnits = false } = {}) {
    const num = Number(value);
    const safeNumber = Number.isFinite(num) ? num : 0;

    const symbol = preset.symbol || "$";

    try {
        const formatted = new Intl.NumberFormat(preset.locale, {
            style: "currency",
            currency: preset.code,
            currencyDisplay: "narrowSymbol",
            // wholeUnits: for tight spots (a calendar day cell's net chip)
            // where "$1,234" fits and "$1,234.56" doesn't.
            minimumFractionDigits: wholeUnits ? 0 : undefined,
            maximumFractionDigits: wholeUnits ? 0 : 2,
            numberingSystem: "latn",
        }).format(wholeUnits ? Math.round(safeNumber) : safeNumber);

        // Some environments show BDT instead of a symbol for Bangladeshi taka
        // Force the taka symbol while keeping locale grouping
        if (preset.code === "BDT") {
            return formatted.replace(/\bBDT\b/g, symbol).replace(/\u00A0/g, " ");
        }

        return formatted.replace(/\u00A0/g, " ");
    } catch {
        return `${symbol}${safeNumber.toFixed(2)}`;
    }
}

// Formats in whichever currency the user currently has selected (Settings,
// or the top-bar switcher) \u2014 for anything already normalized to that
// currency server-side: dashboard/finance stat cards, budgets, chart
// totals. Money the server hands over pre-converted like this needs no
// further conversion here, only formatting.
export function formatMoney(value, options) {
    return _format(getCurrencyPreset(), value, options);
}

// Formats `value` in ITS OWN currency, ignoring whatever the user
// currently has selected \u2014 for a single transaction's own native amount
// (transaction_row.html's .tx-amount, a recurring bill's own row), which
// should always show exactly what was actually paid, in the currency it
// was actually paid in, never silently relabeled with a different
// symbol or converted. Falls back to the globally-selected currency only
// when `currencyCode` itself is missing (an older element that predates
// this attribute) rather than guessing.
export function formatMoneyIn(value, currencyCode, options) {
    return _format(safeGetPreset(currencyCode || getCurrencyCode()), value, options);
}

// Reads data-money-value + data-money-currency off `el` and formats in
// that transaction's own currency \u2014 the one-call shorthand every
// .tx-amount-style call site should use instead of formatMoney(value)
// directly, per formatMoneyIn's own reasoning above.
export function formatMoneyFromElement(el) {
    const raw = el?.dataset?.moneyValue ?? el?.getAttribute?.("data-money-value");
    const currency = el?.dataset?.moneyCurrency ?? el?.getAttribute?.("data-money-currency");
    return formatMoneyIn(Number(raw || 0), currency);
}

export function applyCurrencyToAmountPrefixes() {
    const preset = getCurrencyPreset();
    document.querySelectorAll("[data-currency-symbol]").forEach((el) => {
        el.textContent = preset.symbol || "$";
    });
}