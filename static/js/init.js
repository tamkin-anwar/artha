// static/js/init.js (module)

import "./flash.js";
import "./notes.js";
import "./transactions.js";
import "./theme.js";
import "./settings.js";
import "./ai.js";
import "./auth.js";
import "./scenarios.js";
import "./search.js";
import "./csv_import.js";

import { updateChartData } from "./chart.js";

async function registerServiceWorker() {
    if (!("serviceWorker" in navigator)) return;
    try {
        // Served from the site root (see /service-worker.js in
        // artha/__init__.py) rather than /static/service-worker.js, so its
        // default scope is "/" and it can actually control app pages, not
        // just requests under /static/.
        await navigator.serviceWorker.register("/service-worker.js", { scope: "/" });
    } catch (err) {
        console.warn("Service worker registration failed:", err);
    }
}

function createLoadingIndicator() {
    const el = document.createElement("div");
    el.textContent = "Loading dashboard...";
    el.className = "text-center text-gray-500 my-4 animate-pulse";
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    return el;
}

function createErrorBanner() {
    const el = document.createElement("div");
    el.textContent = "Failed to load dashboard data. Refresh and try again.";
    el.className = "flash-message text-red-500 text-center my-2";
    el.setAttribute("role", "alert");
    return el;
}

function initSettingsMenu() {
    const btn = document.getElementById("settings-btn");
    const menu = document.getElementById("settings-dropdown");

    const accountBtn = document.getElementById("account-btn");
    const accountMenu = document.getElementById("account-dropdown");

    // Opened from the bottom tab bar's "More" tab, not a hamburger
    // button anymore — see #mobile-tabbar in base.html.
    const mobileMenuBtn = document.getElementById("tabbar-more-btn");
    const mobileMenu = document.getElementById("mobile-menu");
    const mobileMenuBackdrop = document.getElementById("mobile-menu-backdrop");
    const mobileMenuCloseBtn = document.getElementById("mobile-menu-close-btn");

    const mobileSettingsBtn = document.getElementById("mobile-settings-btn");
    const mobileSettingsPanel = document.getElementById("mobile-settings-panel");

    const mobileFeedbackBtn = document.getElementById("mobile-feedback-btn");
    const feedbackFab = document.getElementById("feedback-fab");

    if (btn && menu) {
        const closeMenu = () => {
            menu.classList.add("hidden");
            btn.setAttribute("aria-expanded", "false");
        };

        btn.addEventListener("click", () => {
            const isHidden = menu.classList.contains("hidden");
            if (isHidden) {
                menu.classList.remove("hidden");
                btn.setAttribute("aria-expanded", "true");
            } else {
                closeMenu();
            }
        });

        document.addEventListener("click", (e) => {
            if (!menu.contains(e.target) && !btn.contains(e.target)) closeMenu();
        });

        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape") closeMenu();
        });
    }

    // Same open/close/outside-click/Escape pattern as the settings menu
    // above — the user's own name+avatar in the sidebar is the
    // conventional home for identity actions (change password, sign out),
    // same as it would be in Slack, Notion, Linear, etc.
    if (accountBtn && accountMenu) {
        const closeAccountMenu = () => {
            accountMenu.classList.add("hidden");
            accountBtn.setAttribute("aria-expanded", "false");
        };

        accountBtn.addEventListener("click", () => {
            const isHidden = accountMenu.classList.contains("hidden");
            if (isHidden) {
                accountMenu.classList.remove("hidden");
                accountBtn.setAttribute("aria-expanded", "true");
            } else {
                closeAccountMenu();
            }
        });

        document.addEventListener("click", (e) => {
            if (!accountMenu.contains(e.target) && !accountBtn.contains(e.target)) closeAccountMenu();
        });

        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape") closeAccountMenu();
        });
    }

    // Three ways out (X button, backdrop tap, Escape), not just the
    // More tab toggle that used to be the only one — the drawer's own
    // height routinely covers the More tab once it's open, which is
    // exactly the bug a real user hit here, 2026-08-30. Research on
    // bottom-sheet dismissal backs the X button specifically: don't
    // rely on backdrop-tap alone, pair it with an explicit close
    // control. Open/close is now a class toggle (.is-open), not
    // display:none via .hidden, so the drawer/backdrop can fade and
    // slide instead of snapping instantly — see .mobile-menu-panel
    // and .mobile-menu-backdrop in style.css.
    if (mobileMenuBtn && mobileMenu && mobileMenuBackdrop) {
        const openMobileMenu = () => {
            mobileMenu.classList.add("is-open");
            mobileMenuBackdrop.classList.add("is-open");
            mobileMenuBtn.setAttribute("aria-expanded", "true");
            // The menu is a fixed overlay with its own scroll region
            // (see #mobile-menu in style.css), not an in-flow panel —
            // without this, scrolling inside it also scrolled the
            // dashboard visible behind its frosted-glass background.
            document.body.classList.add("mobile-menu-open");
        };
        const closeMobileMenu = () => {
            mobileMenu.classList.remove("is-open");
            mobileMenuBackdrop.classList.remove("is-open");
            mobileMenuBtn.setAttribute("aria-expanded", "false");
            document.body.classList.remove("mobile-menu-open");
        };

        mobileMenuBtn.addEventListener("click", () => {
            if (mobileMenu.classList.contains("is-open")) {
                closeMobileMenu();
            } else {
                openMobileMenu();
            }
        });

        mobileMenuCloseBtn?.addEventListener("click", closeMobileMenu);
        mobileMenuBackdrop.addEventListener("click", closeMobileMenu);

        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape" && mobileMenu.classList.contains("is-open")) closeMobileMenu();
        });

        // The feedback FAB is hidden on mobile (see .feedback-fab's
        // media query in style.css) now that the bottom tab bar sits
        // in the same corner. This forwards to the exact same modal
        // by clicking the real button, rather than duplicating its
        // open logic here.
        if (mobileFeedbackBtn && feedbackFab) {
            mobileFeedbackBtn.addEventListener("click", () => {
                closeMobileMenu();
                feedbackFab.click();
            });
        }
    }

    if (mobileSettingsBtn && mobileSettingsPanel) {
        mobileSettingsBtn.addEventListener("click", () => {
            const isHidden = mobileSettingsPanel.classList.contains("hidden");
            if (isHidden) {
                mobileSettingsPanel.classList.remove("hidden");
                mobileSettingsBtn.setAttribute("aria-expanded", "true");
            } else {
                mobileSettingsPanel.classList.add("hidden");
                mobileSettingsBtn.setAttribute("aria-expanded", "false");
            }
        });
    }
}

async function initDashboardDataWithRetry({ maxRetries = 3, retryDelayMs = 1500 } = {}) {
    let retryCount = 0;

    while (retryCount <= maxRetries) {
        try {
            await updateChartData();
            return { ok: true, retries: retryCount };
        } catch (err) {
            retryCount += 1;
            if (retryCount > maxRetries) {
                return { ok: false, retries: retryCount, error: err };
            }
            await new Promise((resolve) => window.setTimeout(resolve, retryDelayMs));
        }
    }

    return { ok: false, retries: maxRetries + 1 };
}

async function initDashboard() {
    initSettingsMenu();
    await registerServiceWorker();

    const loadingIndicator = createLoadingIndicator();
    document.body.prepend(loadingIndicator);

    const result = await initDashboardDataWithRetry({ maxRetries: 3, retryDelayMs: 1500 });

    loadingIndicator.remove();

    if (result.ok) {
        document.dispatchEvent(new CustomEvent("dashboard-ready", { detail: { retries: result.retries } }));
        return;
    }

    document.body.prepend(createErrorBanner());
}

window.addEventListener("DOMContentLoaded", initDashboard);
