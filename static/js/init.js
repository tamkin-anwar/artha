// static/js/init.js (module)

import "./flash.js";
import "./notes.js";
import "./transactions.js";
import "./theme.js";
import "./settings.js";
import "./ai.js";
import "./auth.js";
import "./scenarios.js";

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

    const mobileMenuBtn = document.getElementById("menu-btn");
    const mobileMenu = document.getElementById("mobile-menu");

    const mobileSettingsBtn = document.getElementById("mobile-settings-btn");
    const mobileSettingsPanel = document.getElementById("mobile-settings-panel");

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

    if (mobileMenuBtn && mobileMenu) {
        mobileMenuBtn.addEventListener("click", () => {
            const isHidden = mobileMenu.classList.contains("hidden");
            if (isHidden) {
                mobileMenu.classList.remove("hidden");
                mobileMenuBtn.setAttribute("aria-expanded", "true");
            } else {
                mobileMenu.classList.add("hidden");
                mobileMenuBtn.setAttribute("aria-expanded", "false");
            }
        });
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
