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

export function formatMoney(value) {
    const preset = getCurrencyPreset();
    const num = Number(value);
    const safeNumber = Number.isFinite(num) ? num : 0;

    const symbol = preset.symbol || "$";

    try {
        const formatted = new Intl.NumberFormat(preset.locale, {
            style: "currency",
            currency: preset.code,
            currencyDisplay: "narrowSymbol",
            maximumFractionDigits: 2,
            numberingSystem: "latn",
        }).format(safeNumber);

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

export function applyCurrencyToAmountPrefixes() {
    const preset = getCurrencyPreset();
    document.querySelectorAll("[data-currency-symbol]").forEach((el) => {
        el.textContent = preset.symbol || "$";
    });
}