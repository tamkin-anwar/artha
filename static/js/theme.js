// static/js/theme.js

let currentTheme = "light"; // Tracks the current theme globally

// Configurable toggle button IDs for easier future extension
const toggleButtonIds = ["toggle-theme", "toggle-theme-mobile"];

// Allows other modules to react when theme changes
export function onThemeChange(callback) {
    document.addEventListener("theme-changed", callback);
}

// Allows removing the theme change listener for cleanup
export function removeThemeChangeListener(callback) {
    document.removeEventListener("theme-changed", callback);
}

// Returns the current theme
export function getCurrentTheme() {
    return currentTheme;
}

// Sets the theme explicitly (used by toggleTheme and programmatic calls)
export function setTheme(theme) {
    applyTheme(theme);
}

// Debounce flag to prevent rapid toggles
let toggleDebounce = false;

// Toggles between light and dark themes using centralized applyTheme
export function toggleTheme() {
    if (toggleDebounce) return; // Ignore if toggling too fast
    toggleDebounce = true;
    const next = currentTheme === "dark" ? "light" : "dark";
    rememberChoice(next);
    applyTheme(next);
    setTimeout(() => { toggleDebounce = false; }, 300);
}

// Artha follows the device's light/dark setting unless the user has
// tapped the toggle. A tap pins a choice under "theme-choice"; tapping
// back to whatever the device is already showing clears the pin, so
// the page goes back to following the device instead of being stuck
// on a choice that only happened to match it. The older "theme" key
// is deliberately not read (the previous version of this file wrote
// the device's setting into it on every first visit, so it can't tell
// a real choice from a default) -- the FOUC script in base.html makes
// the same call before first paint.
const CHOICE_KEY = "theme-choice";
const systemDark = window.matchMedia("(prefers-color-scheme: dark)");

function storedChoice() {
    try {
        const v = localStorage.getItem(CHOICE_KEY);
        return v === "dark" || v === "light" ? v : null;
    } catch (e) {
        return null;
    }
}

function systemTheme() {
    return systemDark.matches ? "dark" : "light";
}

function rememberChoice(theme) {
    try {
        if (theme === systemTheme()) localStorage.removeItem(CHOICE_KEY);
        else localStorage.setItem(CHOICE_KEY, theme);
    } catch (e) {}
}

// The browser chrome's tint (meta theme-color) follows the media query
// on its own; a pinned choice that differs from the device needs both
// tags pointed at the pinned ground instead.
function syncThemeColor(theme) {
    const ground = theme === "dark" ? "#0a0e17" : "#f4f6fa";
    document.querySelectorAll('meta[name="theme-color"]').forEach(meta => {
        if (!meta.dataset.ground) meta.dataset.ground = meta.getAttribute("content");
        meta.setAttribute("content", storedChoice() ? ground : meta.dataset.ground);
    });
}

function loadStoredTheme() {
    setTheme(storedChoice() || systemTheme());
}

// Live: switching the device to dark at sunset updates an open tab,
// unless the user pinned a theme.
systemDark.addEventListener("change", () => {
    if (!storedChoice()) applyTheme(systemTheme());
});

// Init theme logic on DOM ready
document.addEventListener("DOMContentLoaded", () => {
    loadStoredTheme();

    toggleButtonIds.forEach(id => {
        const btn = document.getElementById(id);
        if (btn) {
            btn.addEventListener("click", () => {
                toggleTheme();
            });
        }
    });

    // Dispatch event specifically indicating initialization complete
    document.dispatchEvent(new Event("theme-initialized"));
});

// Returns true if the current theme is dark
export function isDarkTheme() {
    return currentTheme === "dark";
}

// Applies theme programmatically with optional callback suppression
export function applyTheme(theme, suppressEvent = false) {
    try {
        const isDark = theme === "dark";
        document.documentElement.classList.toggle("dark", isDark);
        currentTheme = isDark ? "dark" : "light";
        syncThemeColor(currentTheme);

        // Update aria-pressed to string "true" or "false"
        toggleButtonIds.forEach(id => {
            const btn = document.getElementById(id);
            if (btn) btn.setAttribute("aria-pressed", currentTheme === "dark" ? "true" : "false");
        });

        if (!suppressEvent) {
            document.dispatchEvent(new CustomEvent("theme-changed", { detail: { theme: currentTheme } }));
        }
    } catch (error) {
        console.error("Theme apply failed:", error);
    }
}