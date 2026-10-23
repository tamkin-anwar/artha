// static/js/activity.js: the redesigned Activity page (templates/activity.html).
// A list grouped by day, and an editor for the picked transaction: a
// column beside the list on a wide screen, a sheet on a narrower one.
// Saving and deleting go through the same routes as the rest of the site
// (/update_transaction, /delete_transaction with undo), and a save
// reloads the page, since every total is computed server-side.

import { showToast } from "./toast.js";
import { csrfToken, postJSON, toastAfterReload } from "./shell.js";
import { currencySymbol, formatMoneyIn } from "./currency.js";

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
const endsRow = field("detail-ends-row");
const endsInput = field("detail-ends");
const categoriesBox = field("detail-categories");
const chips = [...categoriesBox.querySelectorAll("[data-category]")];
const errorEl = field("detail-error");

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
intervalSelect.addEventListener("change", () => { endsRow.hidden = !intervalSelect.value; });

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
    field("detail-symbol").textContent = currencySymbol(tx.currency);
    amountInput.value = tx.amount;
    fitAmount();
    amountInput.setAttribute("aria-label", `Amount in ${tx.currency}`);
    field("detail-sub").textContent = longDate(tx.date) + (tx.display_amount ? `, about ${formatMoneyIn(tx.display_amount, page.dataset.currency)}` : "");
    descriptionInput.value = tx.description;
    editor.elements.type.value = tx.type;
    dateInput.value = tx.date || "";
    intervalSelect.value = tx.is_recurring ? (tx.recurrence_interval || "monthly") : "";
    endsRow.hidden = !tx.is_recurring;
    endsInput.value = tx.recurring_end_date || "";
    setCategory(tx.category);
    syncType();
    field("detail-review").hidden = !tx.needs_review;
    field("detail-paired").hidden = !tx.paired_charge;
    resetPairing(tx);
    field("detail-paired-title").textContent = tx.type === "income" ? "Paired with its repeating income" : "Paired with its bill";
    field("detail-paired-text").textContent = tx.paired_charge
        ? `Artha matched \u201C${tx.paired_charge}\u201D to this, so it counts once.`
        : "";
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
    // Repeats: "" is Never. Turning it on or off, or changing how often,
    // goes through update_transaction_fields like every other field.
    const repeating = Boolean(intervalSelect.value);
    if (repeating !== Boolean(current.is_recurring)) body.is_recurring = repeating;
    if (repeating) {
        body.recurrence_interval = intervalSelect.value === "monthly" ? "" : intervalSelect.value;
        // An empty string clears the end date.
        body.recurring_end_date = endsInput.value;
    }

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

// A charge Artha didn't pair: pick the bill it paid from the open ones
// nearby (likeliest first), and it counts once, exactly like an automatic
// pairing (and can be unpaired the same way).
const pairBox = field("detail-pair");
const pairOpen = field("detail-pair-open");
const pairChoices = field("detail-pair-choices");
const pairList = field("detail-pair-list");
const pairEmpty = field("detail-pair-empty");

function shortDate(iso) {
    if (!iso) return "";
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function resetPairing(tx) {
    const income = tx.type === "income";
    pairBox.hidden = !tx.can_pair;
    pairOpen.textContent = income ? "Pair with repeating income" : "Pair with a bill";
    pairOpen.setAttribute("aria-expanded", "false");
    field("detail-pair-question").textContent = income ? "Which repeating income is this?" : "Which bill did this pay?";
    pairChoices.hidden = true;
    pairList.replaceChildren();
    pairEmpty.hidden = true;
}

pairOpen.addEventListener("click", async () => {
    if (!current) return;
    if (!pairChoices.hidden) {
        pairChoices.hidden = true;
        pairOpen.setAttribute("aria-expanded", "false");
        return;
    }
    const tx = current;
    let bills;
    try {
        const res = await fetch(`/activity/${tx.id}/bills`, { headers: { "X-Requested-With": "XMLHttpRequest" } });
        if (!res.ok) throw new Error(String(res.status));
        bills = (await res.json()).bills || [];
    } catch (err) {
        showToast("Artha couldn't load your bills. Try again in a moment.", "error");
        return;
    }
    if (current !== tx) return;
    pairList.replaceChildren(...bills.map((bill) => {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "pair-choice";
        row.setAttribute("role", "listitem");
        const name = document.createElement("span");
        name.className = "pair-name";
        name.textContent = bill.description;
        const meta = document.createElement("span");
        meta.className = "pair-meta";
        meta.textContent = `${shortDate(bill.date)}, ${formatMoneyIn(bill.amount, bill.currency)}`;
        row.append(name, meta);
        row.addEventListener("click", () => pairWith(tx, bill));
        return row;
    }));
    pairList.hidden = bills.length === 0;
    pairEmpty.hidden = bills.length > 0;
    pairEmpty.textContent = tx.type === "income"
        ? "No repeating income is waiting near this date. Repeating income you track shows up here."
        : "No unpaid bills near this date. Bills you track in Plan show up here.";
    pairChoices.hidden = false;
    pairOpen.setAttribute("aria-expanded", "true");
});

async function pairWith(tx, bill) {
    pairList.querySelectorAll(".pair-choice").forEach((b) => { b.disabled = true; });
    try {
        const res = await postJSON(`/activity/${tx.id}/pair`, { bill_id: bill.id });
        if (!res.ok) {
            showToast(res.data.message || "Artha couldn't pair those. Try again in a moment.", "error");
            pairList.querySelectorAll(".pair-choice").forEach((b) => { b.disabled = false; });
            return;
        }
        toastAfterReload(`Paired with ${bill.description}`);
        const url = new URL(window.location.href);
        url.searchParams.set("selected", res.data.bill_id);
        window.location.replace(url);
    } catch (err) {
        showToast("You're offline. Check your connection and try again.", "error");
        pairList.querySelectorAll(".pair-choice").forEach((b) => { b.disabled = false; });
    }
}

// A wrong pairing splits back into the bill (as planned) and the charge
// (under its own name), which Artha then leaves unpaired.
field("detail-unpair").addEventListener("click", async () => {
    if (!current) return;
    if (!window.confirm("Unpair these? Both will count again, and Artha won't pair this charge on its own again.")) return;
    try {
        const res = await postJSON(`/activity/${current.id}/unpair`, {});
        if (!res.ok) {
            showToast(res.data.message || "Artha couldn't split those. Try again in a moment.", "error");
            return;
        }
    } catch (err) {
        showToast("You're offline. Check your connection and try again.", "error");
        return;
    }
    toastAfterReload("Unpaired");
    window.location.reload();
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

// The month filter: every other filter stays as it is.
document.querySelector("[data-month-filter]")?.addEventListener("change", (e) => {
    const url = new URL(window.location.href);
    ["selected", "offset", "partial"].forEach((k) => url.searchParams.delete(k));
    if (e.target.value) url.searchParams.set("month", e.target.value);
    else url.searchParams.delete("month");
    window.location.href = url;
});

// Filing uncategorized transactions: keywords and remembered corrections
// first, the AI only for what's left (finance.routes'
// categorize_uncategorized, rate-limited there). It only ever fills an
// empty category, so it doesn't ask first.
document.querySelector("[data-categorize]")?.addEventListener("click", async (e) => {
    const button = e.currentTarget;
    const label = button.textContent;
    button.disabled = true;
    button.textContent = "Categorizing…";
    try {
        const res = await fetch("/finance/categorize_uncategorized", {
            method: "POST",
            headers: { "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": csrfToken() },
        });
        let data = {};
        try { data = await res.json(); } catch (err) {}
        if (!res.ok) {
            showToast(data.message || "Artha couldn't categorize them right now. Try again in a moment.", "error");
        } else if (data.categorized > 0) {
            toastAfterReload(data.message || "Categorized");
            window.location.reload();
            return;
        } else {
            showToast(data.message || "Nothing to categorize.", "info");
        }
    } catch (err) {
        showToast("You're offline. Check your connection and try again.", "error");
    }
    button.disabled = false;
    button.textContent = label;
});

document.querySelectorAll("[data-import-open]").forEach((b) =>
    b.addEventListener("click", () => document.getElementById("csv-import-btn")?.click()));

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
