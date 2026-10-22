// static/js/shell.js: app-wide pieces of the redesigned shell (2026-10).
// - The Add sheet (partials/add_sheet.html): opened by any [data-add-open]
//   button or N on the keyboard, posted to /add_transaction.
// - The Install Artha card in the sidebar (beforeinstallprompt).
// - A toast that survives the reload a save triggers (every money figure
//   is computed server-side, so after a change the page reloads rather
//   than patching numbers in place; see CLAUDE.md's currency notes).

import { showToast } from "./toast.js";

const TOAST_KEY = "artha-toast-after-reload";

export function toastAfterReload(message, type = "success") {
    try { sessionStorage.setItem(TOAST_KEY, JSON.stringify({ message, type })); } catch (e) {}
}

export function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
}

export async function postJSON(url, body) {
    const res = await fetch(url, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRFToken": csrfToken(),
        },
        body: JSON.stringify(body || {}),
    });
    let data = {};
    try { data = await res.json(); } catch (e) {}
    return { ok: res.ok, status: res.status, data };
}

(function showPendingToast() {
    let pending = null;
    try {
        pending = JSON.parse(sessionStorage.getItem(TOAST_KEY) || "null");
        sessionStorage.removeItem(TOAST_KEY);
    } catch (e) {}
    if (pending && pending.message) showToast(pending.message, pending.type || "success");
})();

function localISODate(d = new Date()) {
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function isTyping(target) {
    if (!target) return false;
    const tag = target.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}

function setupAddSheet() {
    const sheet = document.getElementById("add-sheet");
    const form = document.getElementById("add-form");
    if (!sheet || !form) return;
    const amount = document.getElementById("add-amount");
    const description = document.getElementById("add-description");
    const date = document.getElementById("add-date");
    const errorEl = document.getElementById("add-error");
    const submit = document.getElementById("add-submit");
    const categories = document.getElementById("add-categories");
    const chips = [...categories.querySelectorAll("[data-category]")];
    let category = "";
    let opener = null;

    function setCategory(value) {
        category = value;
        chips.forEach((c) => c.setAttribute("aria-pressed", String(c.dataset.category === value)));
    }

    const endsRow = document.getElementById("add-ends-row");
    function syncRepeat() {
        endsRow.hidden = !form.elements.repeat.value;
    }
    form.elements.repeat.addEventListener("change", syncRepeat);

    function syncType() {
        // Income files itself; the categories are for spending.
        categories.hidden = form.elements.type.value === "income";
    }

    function open(trigger) {
        opener = trigger || document.activeElement;
        form.reset();
        setCategory("");
        syncType();
        syncRepeat();
        date.value = localISODate();
        errorEl.hidden = true;
        sheet.hidden = false;
        document.body.classList.add("sheet-open");
        requestAnimationFrame(() => amount.focus());
    }

    function close() {
        sheet.hidden = true;
        document.body.classList.remove("sheet-open");
        if (opener && typeof opener.focus === "function") opener.focus();
    }

    document.addEventListener("click", (e) => {
        const trigger = e.target.closest("[data-add-open]");
        if (trigger) {
            e.preventDefault();
            open(trigger);
        }
    });
    sheet.querySelectorAll("[data-add-close]").forEach((b) => b.addEventListener("click", close));

    // The installed app's "Add a purchase" shortcut (manifest.json) opens
    // straight into this sheet.
    const params = new URLSearchParams(window.location.search);
    if (params.get("add") === "1") {
        params.delete("add");
        const rest = params.toString();
        history.replaceState(null, "", window.location.pathname + (rest ? `?${rest}` : ""));
        open(null);
    }
    sheet.addEventListener("mousedown", (e) => { if (e.target === sheet) close(); });
    chips.forEach((chip) => chip.addEventListener("click", () => setCategory(chip.dataset.category)));
    form.querySelectorAll('input[name="type"]').forEach((r) => r.addEventListener("change", syncType));

    document.addEventListener("keydown", (e) => {
        if (!sheet.hidden) {
            if (e.key === "Escape") { e.preventDefault(); close(); }
            if (e.key === "Tab") {
                // Keep focus inside the sheet while it's open.
                const focusable = [...form.querySelectorAll("button, input, select, textarea")]
                    .filter((n) => !n.disabled && n.offsetParent !== null);
                const first = focusable[0];
                const last = focusable[focusable.length - 1];
                if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
                else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
            }
            return;
        }
        if ((e.key === "n" || e.key === "N") && !e.metaKey && !e.ctrlKey && !e.altKey && !isTyping(e.target)) {
            // Not while another dialog (search, feedback) is showing.
            if ([...document.querySelectorAll('[role="dialog"]')].some((d) => d.offsetParent !== null)) return;
            e.preventDefault();
            open(null);
        }
    });

    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        errorEl.hidden = true;
        const raw = amount.value.trim().replace(/[^\d.,-]/g, "").replace(/,(?=\d{3}\b)/g, "").replace(",", ".");
        if (!raw || Number(raw) <= 0) {
            errorEl.textContent = "Enter an amount above zero.";
            errorEl.hidden = false;
            amount.focus();
            return;
        }
        if (!description.value.trim()) {
            errorEl.textContent = "Give it a name, like the store or what it was for.";
            errorEl.hidden = false;
            description.focus();
            return;
        }

        const data = new FormData();
        data.append("csrf_token", csrfToken());
        data.append("type", form.elements.type.value);
        data.append("amount", raw);
        data.append("currency", form.elements.currency.value);
        data.append("description", description.value.trim());
        data.append("date", date.value || localISODate());
        if (category && form.elements.type.value === "expense") data.append("category", category);
        const repeat = form.elements.repeat.value;
        if (repeat) {
            data.append("is_recurring", "1");
            if (repeat !== "monthly") data.append("recurrence_interval", repeat);
            if (form.elements.recurring_end_date.value) data.append("recurring_end_date", form.elements.recurring_end_date.value);
        }

        submit.disabled = true;
        try {
            const res = await fetch("/add_transaction", {
                method: "POST",
                headers: { "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": csrfToken() },
                body: data,
            });
            if (!res.ok) {
                let message = "Artha couldn't add that. Try again in a moment.";
                try { message = (await res.json()).message || message; } catch (err) {}
                errorEl.textContent = message;
                errorEl.hidden = false;
                return;
            }
            toastAfterReload("Added");
            window.location.reload();
        } catch (err) {
            errorEl.textContent = "You're offline. Check your connection and try again.";
            errorEl.hidden = false;
        } finally {
            submit.disabled = false;
        }
    });
}

function setupInstallCard() {
    const card = document.getElementById("install-card");
    if (!card) return;
    const DISMISS_KEY = "artha-install-dismissed";
    let deferred = null;
    let dismissed = false;
    try { dismissed = localStorage.getItem(DISMISS_KEY) === "1"; } catch (e) {}

    window.addEventListener("beforeinstallprompt", (e) => {
        e.preventDefault();
        deferred = e;
        if (!dismissed) card.hidden = false;
    });
    window.addEventListener("appinstalled", () => { card.hidden = true; });
    document.getElementById("install-btn")?.addEventListener("click", async () => {
        if (!deferred) return;
        deferred.prompt();
        await deferred.userChoice.catch(() => null);
        deferred = null;
        card.hidden = true;
    });
    document.getElementById("install-dismiss")?.addEventListener("click", () => {
        try { localStorage.setItem(DISMISS_KEY, "1"); } catch (e) {}
        card.hidden = true;
    });
}

// "Send feedback" lives in the sidebar footer, the phone's More drawer and
// Settings; each forwards to the one feedback dialog (base.html), whose
// floating button no longer shows.
document.addEventListener("click", (e) => {
    if (e.target.closest("[data-feedback-open]")) document.getElementById("feedback-fab")?.click();
});

setupAddSheet();
setupInstallCard();
