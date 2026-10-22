// static/js/activity.js: the redesigned Activity page (templates/activity.html).
// A list grouped by day, and an editor for the picked transaction: a
// column beside the list on a wide screen, a sheet on a narrower one.
// Saving and deleting go through the same routes as the rest of the site
// (/update_transaction, /delete_transaction with undo), and a save
// reloads the page, since every total is computed server-side.

import { showToast } from "./toast.js";
import { csrfToken, postJSON, toastAfterReload } from "./shell.js";
import { formatMoneyIn } from "./currency.js";

const page = document.getElementById("activity");
const days = document.getElementById("activity-days");
const detail = document.getElementById("activity-detail");
const editor = document.getElementById("tx-editor");
const empty = document.getElementById("detail-empty");
const wide = window.matchMedia("(min-width: 1100px)");

const field = (id) => document.getElementById(id);
const amountInput = field("detail-amount");
const descriptionInput = field("detail-description");
const dateInput = field("detail-date");
const intervalSelect = field("detail-interval");
const repeatRow = field("detail-repeat-row");
const categoriesBox = field("detail-categories");
const chips = [...categoriesBox.querySelectorAll("[data-category]")];
const errorEl = field("detail-error");

const SYMBOLS = { USD: "$", GBP: "£", EUR: "€", BDT: "৳", CAD: "$", AUD: "$" };
const SOURCES = {
    apple_pay: "Logged on its own with Apple Pay.",
    csv: "Imported from a bank statement.",
};

let current = null;
let category = null;
let lastRow = null;

function initials(name) {
    const words = (name || "").trim().split(/\s+/).filter(Boolean);
    if (words.length > 1) return (words[0][0] + words[1][0]).toUpperCase();
    return (name || "?").slice(0, 2).toUpperCase();
}

function longDate(iso) {
    if (!iso) return "";
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric", year: "numeric" });
}

// The amount field is as wide as what's typed, so the currency symbol
// sits right against the number.
function fitAmount() {
    amountInput.style.width = `${Math.max(2, amountInput.value.length) + 0.4}ch`;
}
amountInput.addEventListener("input", fitAmount);

function setCategory(value) {
    category = value;
    chips.forEach((c) => c.setAttribute("aria-pressed", String(c.dataset.category === value)));
}

function syncType() {
    categoriesBox.hidden = editor.elements.type.value === "income";
}

function isSheet() {
    return !wide.matches;
}

function select(row, { push = true } = {}) {
    let tx;
    try { tx = JSON.parse(row.dataset.tx); } catch (e) { return; }
    current = tx;
    lastRow = row;
    document.querySelectorAll(".activity-row.is-selected").forEach((r) => r.classList.remove("is-selected"));
    row.classList.add("is-selected");
    row.setAttribute("aria-current", "true");

    field("detail-mark").textContent = initials(tx.description);
    field("detail-symbol").textContent = SYMBOLS[tx.currency] || "";
    amountInput.value = tx.amount;
    fitAmount();
    amountInput.setAttribute("aria-label", `Amount in ${tx.currency}`);
    field("detail-sub").textContent = longDate(tx.date) + (tx.display_amount ? `, about ${formatMoneyIn(tx.display_amount, page.dataset.currency)}` : "");
    descriptionInput.value = tx.description;
    editor.elements.type.value = tx.type;
    dateInput.value = tx.date || "";
    repeatRow.hidden = !tx.is_recurring;
    intervalSelect.value = tx.recurrence_interval === "monthly" ? "" : (tx.recurrence_interval || "");
    setCategory(tx.category);
    syncType();
    field("detail-review").hidden = !tx.needs_review;
    field("detail-source").textContent = SOURCES[tx.source] || "";
    errorEl.hidden = true;

    empty.hidden = true;
    editor.hidden = false;
    if (isSheet()) {
        detail.classList.add("is-open");
        document.body.classList.add("sheet-open");
        // Focus the sheet, not a field: on a phone a focused field pops
        // the keyboard over what you came to look at.
        requestAnimationFrame(() => detail.querySelector(".detail-close").focus());
    }
    if (push) {
        const url = new URL(window.location.href);
        url.searchParams.set("selected", tx.id);
        history.replaceState(null, "", url);
    }
}

function closeDetail() {
    detail.classList.remove("is-open");
    document.body.classList.remove("sheet-open");
    if (isSheet()) {
        editor.hidden = true;
        empty.hidden = false;
        document.querySelectorAll(".activity-row.is-selected").forEach((r) => r.classList.remove("is-selected"));
        const url = new URL(window.location.href);
        url.searchParams.delete("selected");
        history.replaceState(null, "", url);
        if (lastRow) lastRow.focus();
    }
}

days.addEventListener("click", (e) => {
    const row = e.target.closest(".activity-row");
    if (row) {
        e.preventDefault();
        select(row);
    }
});

chips.forEach((chip) => chip.addEventListener("click", () => setCategory(chip.dataset.category)));
editor.querySelectorAll('input[name="type"]').forEach((r) => r.addEventListener("change", syncType));
document.querySelectorAll("[data-detail-close]").forEach((b) => b.addEventListener("click", closeDetail));
detail.addEventListener("mousedown", (e) => { if (e.target === detail && isSheet()) closeDetail(); });
document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && detail.classList.contains("is-open")) closeDetail();
});

editor.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!current) return;
    errorEl.hidden = true;
    const amount = amountInput.value.trim().replace(/[^\d.,]/g, "").replace(/,(?=\d{3}\b)/g, "").replace(",", ".");
    if (!amount || Number(amount) <= 0) {
        errorEl.textContent = "Enter an amount above zero.";
        errorEl.hidden = false;
        amountInput.focus();
        return;
    }
    if (!descriptionInput.value.trim()) {
        errorEl.textContent = "Give it a name.";
        errorEl.hidden = false;
        descriptionInput.focus();
        return;
    }
    const body = {
        description: descriptionInput.value.trim(),
        amount,
        type: editor.elements.type.value,
        date: dateInput.value,
    };
    // Only send a category when one is picked; leaving it out keeps
    // whatever is there (update_transaction_fields' own rule).
    if (body.type === "expense" && category) body.category = category;
    if (body.type === "income") body.category = "income";
    if (current.is_recurring) body.recurrence_interval = intervalSelect.value;

    const save = field("detail-save");
    save.disabled = true;
    try {
        const res = await postJSON(`/update_transaction/${current.id}`, body);
        if (!res.ok) {
            errorEl.textContent = res.data.message || "Artha couldn't save that. Try again in a moment.";
            errorEl.hidden = false;
            return;
        }
        toastAfterReload("Saved");
        // On a phone the sheet shouldn't pop back open after the reload.
        const url = new URL(window.location.href);
        if (isSheet()) url.searchParams.delete("selected");
        window.location.replace(url);
    } catch (err) {
        errorEl.textContent = "You're offline. Check your connection and try again.";
        errorEl.hidden = false;
    } finally {
        save.disabled = false;
    }
});

field("detail-delete").addEventListener("click", async () => {
    if (!current) return;
    const tx = current;
    // No "are you sure": the toast offers Undo instead.
    try {
        const res = await fetch(`/delete_transaction/${tx.id}`, {
            method: "POST",
            headers: { "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": csrfToken() },
        });
        if (!res.ok) {
            showToast("Artha couldn't delete that. Try again in a moment.", "error");
            return;
        }
    } catch (err) {
        showToast("You're offline. Check your connection and try again.", "error");
        return;
    }
    const row = days.querySelector(`[data-tx-id="${tx.id}"]`);
    const day = row?.closest(".day");
    row?.remove();
    if (day && !day.querySelector(".activity-row")) day.remove();
    current = null;
    editor.hidden = true;
    empty.hidden = false;
    detail.classList.remove("is-open");
    document.body.classList.remove("sheet-open");
    showToast(tx.is_recurring ? "Deleted, and it stops repeating" : "Deleted", "success", 8000, {
        actionText: "Undo",
        onAction: async () => {
            const undo = await postJSON("/undo_delete_transaction", {});
            if (undo.ok) {
                toastAfterReload("Restored");
                window.location.reload();
            } else {
                showToast(undo.data.message || "Undo failed", "error");
            }
        },
    });
});

field("detail-looks-right").addEventListener("click", async () => {
    if (!current) return;
    const res = await postJSON("/activity/reviewed", { ids: [current.id] });
    if (res.ok) {
        toastAfterReload("Marked reviewed");
        window.location.reload();
    }
});

document.querySelector("[data-mark-reviewed]")?.addEventListener("click", async () => {
    const ids = [...days.querySelectorAll(".activity-row")].map((r) => Number(r.dataset.txId));
    const res = await postJSON("/activity/reviewed", { ids });
    if (res.ok) {
        toastAfterReload(res.data.to_review ? `${res.data.to_review} still to review` : "All reviewed");
        window.location.href = "/activity";
    }
});

// "Show more": the next page of rows, merging a day split across pages.
days.addEventListener("click", async (e) => {
    const button = e.target.closest("[data-show-more]");
    if (!button) return;
    const holder = button.closest(".show-more");
    button.disabled = true;
    const url = new URL(window.location.href);
    url.searchParams.delete("selected");
    url.searchParams.set("partial", "1");
    url.searchParams.set("offset", holder.dataset.nextOffset);
    try {
        const res = await fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } });
        const html = await res.text();
        const incoming = document.createElement("div");
        incoming.innerHTML = html;
        holder.remove();
        const lastDay = days.querySelector(".day:last-of-type");
        const firstNew = incoming.querySelector(".day");
        if (lastDay && firstNew && firstNew.dataset.day === lastDay.dataset.day) {
            firstNew.querySelectorAll(".activity-row").forEach((r) => lastDay.append(r));
            const total = lastDay.querySelector("[data-day-total]");
            const added = Number(firstNew.querySelector("[data-day-total]").dataset.dayTotal || 0);
            const sum = Number(total.dataset.dayTotal || 0) + added;
            total.dataset.dayTotal = String(sum);
            total.textContent = sum ? formatMoneyIn(sum, page.dataset.currency) : "";
            firstNew.remove();
        }
        days.append(...incoming.childNodes);
        if (window.lucide) window.lucide.createIcons();
    } catch (err) {
        button.disabled = false;
        showToast("You're offline. Check your connection and try again.", "error");
    }
});

// Opening on a picked transaction (from Today's Recent, or a reload
// after saving): select it, without stealing focus on a wide screen.
const preselected = page.dataset.selected && days.querySelector(`[data-tx-id="${page.dataset.selected}"]`);
if (preselected) {
    select(preselected, { push: false });
    if (!isSheet()) preselected.scrollIntoView({ block: "nearest" });
} else if (wide.matches) {
    const first = days.querySelector(".activity-row");
    if (first) select(first, { push: false });
}
