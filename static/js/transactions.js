// static/js/transactions.js
import { updateChartData } from "./chart.js";
import { showToast } from "./toast.js";
import { formatMoney } from "./currency.js";

let ariaLiveRegion = null;
let saveTimeout = null;

function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
}

function csrfHeaders() {
    const token = getCsrfToken();
    if (!token) {
        console.warn("[CSRF] Missing meta csrf-token. base.html may be cached or stale.");
    }
    return {
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRFToken": token,
        "X-CSRF-Token": token,
    };
}

function debounceSaveTransaction(e) {
    clearTimeout(saveTimeout);
    saveTimeout = setTimeout(() => saveTransaction(e), 300);
}

function applyAmountTypeDataset(amountEl, type) {
    if (!amountEl) return;
    amountEl.dataset.type = type;
    const icon = amountEl.closest("li[data-id]")?.querySelector(".tx-flow-icon");
    if (icon) icon.dataset.type = type;
}

function parseEditableMoneyToNumber(text) {
    const cleaned = (text || "").toString().trim().replace(/[^\d.-]/g, "");
    const num = Number.parseFloat(cleaned);
    return Number.isFinite(num) ? num : null;
}

function formatRowMoney(row) {
    const amountEl = row?.querySelector?.(".tx-amount");
    if (!amountEl) return;

    const rawAttr = amountEl.getAttribute("data-money-value");
    if (rawAttr !== null && rawAttr !== "") {
        const num = parseFloat(rawAttr);
        if (Number.isFinite(num)) {
            amountEl.textContent = formatMoney(num);
            return;
        }
    }

    const rawText = amountEl.textContent.trim().replace(/[^\d.-]/g, "");
    const num = parseFloat(rawText);
    if (Number.isFinite(num)) {
        amountEl.textContent = formatMoney(num);
        amountEl.setAttribute("data-money-value", String(num));
    }
}

async function updateSummaryUI() {
    const incomeEl = document.getElementById("finance-income");
    const expenseEl = document.getElementById("finance-expense");
    const balanceEl = document.getElementById("finance-balance");
    if (!incomeEl || !expenseEl || !balanceEl) return;

    try {
        const res = await fetch("/api/finance_totals", {
            headers: { "X-Requested-With": "XMLHttpRequest" },
            credentials: "same-origin",
        });
        if (!res.ok) throw new Error("Failed to fetch totals");

        const data = await res.json();

        incomeEl.dataset.moneyValue = data.income;
        expenseEl.dataset.moneyValue = data.expense;
        balanceEl.dataset.moneyValue = data.balance;

        incomeEl.textContent = formatMoney(data.income);
        expenseEl.textContent = formatMoney(data.expense);
        balanceEl.textContent = formatMoney(data.balance);
    } catch (err) {
        console.warn("Could not update summary UI:", err);
    }
}

// -----------------------------------------------------------------------
// Refresh the stat cards / summary card / trend chart after an inline
// edit or delete changes the numbers behind them. The transaction list
// itself is already updated in place by the caller, so this deliberately
// leaves #tx-list alone — it re-fetches the current finance page (same
// ?month= filter the user is currently looking at) and pulls the fresh
// values out of the returned HTML rather than doing a full navigation.
// -----------------------------------------------------------------------

const FINANCIAL_SUMMARY_IDS = [
    "fp-stat-income",
    "fp-stat-expense",
    "fp-stat-net",
    "fp-stat-tx-count",
    "fp-savings-rate",
    "fp-biggest-category",
    "fp-biggest-day",
];

async function refreshFinancialSummary() {
    // Only meaningful on the full finance page — the dashboard's small
    // finance widget doesn't have any of these elements.
    if (!document.getElementById("fp-stat-income")) return;

    try {
        const url = window.location.pathname + window.location.search;
        const res = await fetch(url, {
            credentials: "same-origin",
            headers: { "X-Requested-With": "XMLHttpRequest" },
        });
        if (!res.ok) return;

        const html = await res.text();
        const freshDoc = new DOMParser().parseFromString(html, "text/html");

        FINANCIAL_SUMMARY_IDS.forEach((id) => {
            const freshEl = freshDoc.getElementById(id);
            const liveEl = document.getElementById(id);
            if (freshEl && liveEl) {
                liveEl.textContent = freshEl.textContent;
            }
        });

        const chart = window.__monthlyTrendChart;
        const freshCanvas = freshDoc.getElementById("monthlyTrendChart");
        if (chart && freshCanvas) {
            let trendData = [];
            try {
                trendData = JSON.parse(freshCanvas.dataset.trend || "[]");
            } catch (parseErr) {
                console.warn("Could not parse refreshed trend data:", parseErr);
            }

            const labels = trendData.map((d) => d.label);
            const values = trendData.map((d) => d.net);

            const getVar = (name, fallback) => {
                const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
                return v || fallback;
            };
            const emerald = getVar("--emerald", "#22c55e");
            const red = getVar("--red", "#ef4444");
            const barColors = values.map((v) => (v >= 0 ? emerald : red));

            chart.data.labels = labels;
            chart.data.datasets[0].data = values;
            chart.data.datasets[0].backgroundColor = barColors;
            chart.data.datasets[1].data = values;
            chart.update();
        }
    } catch (err) {
        console.warn("Could not refresh financial summary:", err);
    }
}

// -----------------------------------------------------------------------
// Date-based sorting & grouping — replaces manual drag-to-reorder. Rows
// are always displayed newest-first by their data-date attribute, with a
// divider inserted above the first row of each distinct date.
// -----------------------------------------------------------------------

function formatDateLabel(dateStr) {
    const parts = (dateStr || "").split("-").map(Number);
    if (parts.length !== 3 || parts.some((n) => !Number.isFinite(n))) return dateStr || "";
    const [y, m, d] = parts;
    // Noon UTC avoids the label flipping to the previous/next day depending
    // on the viewer's local timezone offset.
    const dt = new Date(Date.UTC(y, m - 1, d, 12));
    return dt.toLocaleDateString("en-US", { month: "short", day: "2-digit", year: "numeric", timeZone: "UTC" });
}

function buildDateDivider(dateStr, isFirst) {
    const li = document.createElement("li");
    li.className = "tx-date-divider";
    li.style.listStyle = "none";

    const wrap = document.createElement("div");
    wrap.style.cssText = `color:var(--text-muted); font-size:11px; display:flex; align-items:center; gap:8px; margin:${isFirst ? "0" : "16px"} 0 8px;`;

    const lineLeft = document.createElement("span");
    lineLeft.style.cssText = "flex:1; height:1px; background:var(--border-subtle);";

    const label = document.createElement("span");
    label.textContent = formatDateLabel(dateStr);

    const lineRight = document.createElement("span");
    lineRight.style.cssText = "flex:1; height:1px; background:var(--border-subtle);";

    wrap.appendChild(lineLeft);
    wrap.appendChild(label);
    wrap.appendChild(lineRight);
    li.appendChild(wrap);
    return li;
}

function resortTransactionRows() {
    const list = document.getElementById("tx-list");
    if (!list) return;

    const rows = Array.from(list.querySelectorAll("li[data-id]"));
    if (!rows.length) return;

    rows.sort((a, b) => (a.dataset.date || "").localeCompare(b.dataset.date || "")); // oldest first

    // Rebuild from scratch: drop stale dividers, then re-append rows in
    // the new ascending order, with a fresh divider inserted wherever the
    // date changes (i.e. above the first row of each date group — now the
    // oldest group at the top, newest group at the bottom).
    list.querySelectorAll(".tx-date-divider").forEach((el) => el.remove());

    let lastDate = null;
    rows.forEach((row) => {
        const rowDate = row.dataset.date || "";
        if (rowDate !== lastDate) {
            list.appendChild(buildDateDivider(rowDate, lastDate === null));
            lastDate = rowDate;
        }
        list.appendChild(row);
    });
}

async function undoDeleteTransaction() {
    try {
        const res = await fetch("/undo_delete_transaction", {
            method: "POST",
            credentials: "same-origin",
            headers: csrfHeaders(),
        });

        const data = await res.json().catch(() => ({}));

        if (!res.ok) {
            const msg = data?.message || "Undo failed";
            showToast(msg, "error");
            if (ariaLiveRegion) ariaLiveRegion.textContent = msg;
            return;
        }

        const html = data?.row_html || "";
        const tempDiv = document.createElement("div");
        tempDiv.innerHTML = html;

        const restoredRow = tempDiv.querySelector("li[data-id]");
        const list = document.getElementById("tx-list");

        if (restoredRow && list) {
            list.appendChild(restoredRow);

            const typeSelect = restoredRow.querySelector(".tx-type");
            const amountEl = restoredRow.querySelector(".tx-amount");
            if (typeSelect && amountEl) {
                applyAmountTypeDataset(amountEl, typeSelect.value);
                formatRowMoney(restoredRow);
            }

            attachRowListeners(restoredRow);
            resortTransactionRows();

            const msg = data?.message || "Transaction restored.";
            showToast(msg, "success");
            if (ariaLiveRegion) ariaLiveRegion.textContent = msg;
        } else {
            showToast("Transaction restored, but UI could not render the row.", "error");
        }

        await updateChartData();
        await updateSummaryUI();
        document.dispatchEvent(new CustomEvent("currency-refresh-ui"));
    } catch (err) {
        console.error("Undo delete transaction error:", err);
        showToast("Network error while undoing delete", "error");
        if (ariaLiveRegion) ariaLiveRegion.textContent = "Network error while undoing delete";
    }
}

function attachDeleteListener(row) {
    const deleteForm = row.querySelector(".tx-delete-form");
    if (!deleteForm) return;

    if (deleteForm.dataset.bound === "1") return;
    deleteForm.dataset.bound = "1";

    deleteForm.addEventListener("submit", async (e) => {
        e.preventDefault();

        const rowEl = deleteForm.closest("li[data-id]");
        if (!rowEl) return;

        rowEl.setAttribute("aria-busy", "true");
        rowEl.classList.add("bg-yellow-100");

        try {
            const res = await fetch(deleteForm.action, {
                method: "POST",
                credentials: "same-origin",
                headers: csrfHeaders(),
            });

            const data = await res.json().catch(() => ({}));

            if (!res.ok) {
                const msg = data?.message || "Delete failed";
                rowEl.classList.remove("bg-yellow-100");
                rowEl.classList.add("bg-red-100");
                rowEl.removeAttribute("aria-busy");
                setTimeout(() => rowEl.classList.remove("bg-red-100"), 1000);

                showToast(msg, "error");
                if (ariaLiveRegion) ariaLiveRegion.textContent = msg;
                return;
            }

            rowEl.remove();
            resortTransactionRows(); // clears any now-orphaned date divider

            const msg = data?.message || "Transaction deleted";
            if (ariaLiveRegion) ariaLiveRegion.textContent = msg;

            if (data?.can_undo) {
                showToast("Transaction deleted", "info", 10000, {
                    actionText: "Undo",
                    onAction: () => undoDeleteTransaction(),
                });
            } else {
                showToast(msg, "info");
            }

            await updateChartData();
            await updateSummaryUI();
            await refreshFinancialSummary();
            document.dispatchEvent(new CustomEvent("currency-refresh-ui"));
        } catch (err) {
            console.error("Network error deleting transaction:", err);

            rowEl.classList.remove("bg-yellow-100");
            rowEl.classList.add("bg-red-100");
            rowEl.removeAttribute("aria-busy");
            setTimeout(() => rowEl.classList.remove("bg-red-100"), 1000);

            showToast("Network error while deleting transaction", "error");
            if (ariaLiveRegion) ariaLiveRegion.textContent = "Network error while deleting transaction";
        }
    });
}

function applyRecurringState(row, isRecurring) {
    const btn = row.querySelector(".tx-recurring-toggle");
    if (btn) {
        btn.classList.toggle("tx-recurring-on", isRecurring);
        btn.setAttribute("aria-pressed", String(isRecurring));
        btn.title = isRecurring ? "Recurring — click to turn off" : "Mark as recurring";
        btn.textContent = isRecurring ? "↻" : "";
    }

    row.style.borderLeft = isRecurring ? "3px solid var(--gold)" : "";

    const metaRow = row.querySelector(".tx-meta-row");
    let label = row.querySelector(".tx-recurring-label");

    if (isRecurring && !label && metaRow) {
        label = document.createElement("span");
        label.className = "tx-recurring-label";
        label.style.color = "var(--gold)";
        label.textContent = "· ↻ recurring";
        metaRow.appendChild(label);
    } else if (!isRecurring && label) {
        label.remove();
    }
}

async function toggleRecurring(row) {
    const btn = row.querySelector(".tx-recurring-toggle");
    if (!btn) return;
    const id = btn.dataset.id;

    try {
        const res = await fetch(`/finance/transaction/${id}/toggle-recurring`, {
            method: "PATCH",
            credentials: "same-origin",
            headers: csrfHeaders(),
        });

        const data = await res.json().catch(() => ({}));

        if (!res.ok) {
            showToast(data?.message || "Could not update recurring status", "error");
            return;
        }

        applyRecurringState(row, !!data.is_recurring);
    } catch (err) {
        console.error("Network error toggling recurring:", err);
        showToast("Network error while updating recurring status", "error");
    }
}

function attachRecurringToggleListener(row) {
    const btn = row.querySelector(".tx-recurring-toggle");
    if (!btn) return;
    if (btn.dataset.bound === "1") return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => toggleRecurring(row));
}

function attachRowListeners(row) {
    const desc = row.querySelector(".tx-desc");
    const amount = row.querySelector(".tx-amount");
    const typeSelect = row.querySelector(".tx-type");
    const dateInput = row.querySelector(".tx-date");
    const editForm = row.querySelector("form:not(.tx-delete-form)");

    attachDeleteListener(row);
    attachRecurringToggleListener(row);

    // Belt-and-braces: the row has a hidden submit button, and a native
    // <input type="date"> (unlike the contenteditable desc/amount spans)
    // submits its form on Enter. Without this, that submit bypasses fetch
    // entirely and the browser navigates to the raw JSON response. This
    // catches *any* trigger of a native submit on this form, not just the
    // date field, and always routes it through the same fetch-based save.
    if (editForm && editForm.dataset.submitBound !== "1") {
        editForm.dataset.submitBound = "1";
        editForm.addEventListener("submit", (e) => {
            e.preventDefault();
            clearTimeout(saveTimeout);
            saveTransaction(e);
        });
    }

    if (desc) {
        desc.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                desc.blur();
            }
        });
        desc.addEventListener("blur", debounceSaveTransaction);
    }

    if (amount) {
        amount.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                amount.blur();
            }
        });
        amount.addEventListener("blur", debounceSaveTransaction);
    }

    if (typeSelect) {
        typeSelect.addEventListener("change", (e) => {
            const rowEl = e.target.closest("li[data-id]");
            const amountEl = rowEl?.querySelector(".tx-amount");
            applyAmountTypeDataset(amountEl, e.target.value);
            debounceSaveTransaction(e);
        });
    }

    if (dateInput) {
        dateInput.addEventListener("change", (e) => {
            debounceSaveTransaction(e);
        });
    }
}

async function saveTransaction(e) {
    const row = e.target.closest("li[data-id]");
    if (!row) return;

    const id = row.dataset.id;
    const descEl = row.querySelector(".tx-desc");
    const amountEl = row.querySelector(".tx-amount");
    const typeSelect = row.querySelector(".tx-type");
    const dateInput = row.querySelector(".tx-date");
    if (!descEl || !amountEl || !typeSelect) return;

    const dateBeforeSave = row.dataset.date;
    const desc = descEl.textContent.trim();
    const type = typeSelect.value;
    const dateValue = dateInput ? dateInput.value : "";

    const parsed = parseEditableMoneyToNumber(amountEl.textContent);
    if (parsed === null) {
        showToast("Invalid amount entered", "error");
        amountEl.textContent = formatMoney(Number(amountEl.dataset.moneyValue || 0));
        return;
    }

    row.classList.add("bg-yellow-100");
    row.setAttribute("aria-busy", "true");

    try {
        const res = await fetch(`/update_transaction/${id}`, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                ...csrfHeaders(),
            },
            body: JSON.stringify({ description: desc, amount: parsed, type, date: dateValue }),
        });

        const responseData = await res.json().catch(() => ({}));

        if (!res.ok) {
            row.classList.remove("bg-yellow-100");
            row.classList.add("bg-red-100");
            row.removeAttribute("aria-busy");
            setTimeout(() => row.classList.remove("bg-red-100"), 1000);

            const errorMsg = responseData?.message || "Transaction update failed";
            showToast(errorMsg, "error");
            if (ariaLiveRegion) ariaLiveRegion.textContent = errorMsg;

            amountEl.textContent = formatMoney(Number(amountEl.dataset.moneyValue || 0));
            return;
        }

        row.classList.remove("bg-yellow-100");
        row.classList.add("bg-green-100");
        row.removeAttribute("aria-busy");
        setTimeout(() => row.classList.remove("bg-green-100"), 1000);

        amountEl.dataset.moneyValue = String(parsed);
        amountEl.textContent = formatMoney(parsed);
        applyAmountTypeDataset(amountEl, type);

        if (responseData.date) {
            // Update the row's underlying date + visible label in place.
            // Only resort when the date itself actually changed — editing
            // just the description/amount/type still skips the resort, so
            // the row doesn't jump position mid-edit for unrelated fields.
            // But an actual date change means the row is now in the wrong
            // bucket/divider, so it does need to move.
            const dateChanged = responseData.date !== dateBeforeSave;

            row.dataset.date = responseData.date;
            if (dateInput) dateInput.value = responseData.date;
            const dateLabel = row.querySelector(".tx-date-label");
            if (dateLabel && responseData.date_label) {
                dateLabel.textContent = responseData.date_label;
            }

            if (dateChanged) {
                resortTransactionRows();
            }
        }

        const successMsg = responseData?.message || "Transaction updated successfully";
        showToast(successMsg, "success", 1500);
        if (ariaLiveRegion) ariaLiveRegion.textContent = successMsg;

        await updateChartData();
        await updateSummaryUI();
        await refreshFinancialSummary();
        document.dispatchEvent(new CustomEvent("currency-refresh-ui"));
    } catch (err) {
        console.error("Network error updating transaction:", err);

        row.classList.remove("bg-yellow-100");
        row.classList.add("bg-red-100");
        row.removeAttribute("aria-busy");
        setTimeout(() => row.classList.remove("bg-red-100"), 1000);

        showToast("Network error while updating transaction", "error");
        if (ariaLiveRegion) ariaLiveRegion.textContent = "Network error while updating transaction";

        amountEl.textContent = formatMoney(Number(amountEl.dataset.moneyValue || 0));
    }
}

function handleAddTransactionForm(form) {
    form.addEventListener("submit", async (e) => {
        e.preventDefault();

        const csrfToken = getCsrfToken();
        const formData = new FormData(form);

        const description = (formData.get("description") || "").trim();
        const amountStr = (formData.get("amount") || "").toString().trim();
        const amountNum = Number.parseFloat(amountStr);

        if (!description || !Number.isFinite(amountNum)) {
            showToast("Please provide valid details", "error");
            return;
        }

        if (!formData.get("csrf_token") && csrfToken) {
            formData.append("csrf_token", csrfToken);
        }

        try {
            const res = await fetch(form.action, {
                method: "POST",
                credentials: "same-origin",
                headers: csrfHeaders(),
                body: formData,
            });

            if (!res.ok) {
                const maybeJson = await res.json().catch(() => null);
                const msg = maybeJson?.message || "Error adding transaction";
                showToast(msg, "error");
                return;
            }

            const html = await res.text();
            const tempDiv = document.createElement("div");
            tempDiv.innerHTML = html;

            const newRow = tempDiv.querySelector("li[data-id]");
            const list = document.getElementById("tx-list");

            if (!newRow || !list) {
                showToast("Transaction added but UI could not render the row", "error");
                return;
            }

            const amountEl = newRow.querySelector(".tx-amount");
            if (amountEl) {
                amountEl.dataset.moneyValue = String(amountNum);
                amountEl.textContent = formatMoney(amountNum);
            }

            const typeSelect = newRow.querySelector(".tx-type");
            if (typeSelect && amountEl) applyAmountTypeDataset(amountEl, typeSelect.value);

            list.appendChild(newRow);
            attachRowListeners(newRow);
            formatRowMoney(newRow);
            resortTransactionRows();

            showToast("Transaction added!", "success", 1500);

            await updateChartData();
            await updateSummaryUI();
            document.dispatchEvent(new CustomEvent("currency-refresh-ui"));

            const dateInput = form.querySelector("#tx-date-input");
            const todayValue = new Date().toISOString().split("T")[0];
            form.reset();
            // form.reset() would blank the date field back to nothing —
            // put it back on today rather than leaving it empty.
            if (dateInput) dateInput.value = todayValue;
        } catch (err) {
            console.error("Error adding transaction:", err);
            showToast("Error adding transaction", "error");
        }
    });
}

function applyAllTransactionDatasets() {
    const list = document.getElementById("tx-list");
    if (!list) return;

    list.querySelectorAll("li[data-id]").forEach((row) => {
        const typeSelect = row.querySelector(".tx-type");
        const amountEl = row.querySelector(".tx-amount");
        if (typeSelect && amountEl) {
            applyAmountTypeDataset(amountEl, typeSelect.value);
            formatRowMoney(row);
        }
    });
}

function initTransactions() {
    const list = document.getElementById("tx-list");
    if (!list) return;

    if (!ariaLiveRegion) {
        ariaLiveRegion = document.createElement("div");
        ariaLiveRegion.setAttribute("aria-live", "assertive");
        ariaLiveRegion.classList.add("sr-only");
        document.body.appendChild(ariaLiveRegion);
    }

    list.querySelectorAll("li[data-id]").forEach((row) => attachRowListeners(row));
    applyAllTransactionDatasets();

    const addTransactionForm = document.querySelector('form[action*="add_transaction"]');
    if (addTransactionForm) handleAddTransactionForm(addTransactionForm);

    const dateInput = document.getElementById("tx-date-input");
    if (dateInput) {
        dateInput.value = new Date().toISOString().split("T")[0];
    }

    updateSummaryUI();

    document.dispatchEvent(new CustomEvent("currency-refresh-ui"));
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initTransactions);
} else {
    initTransactions();
}

document.addEventListener("currency-refresh-ui", async () => {
    const list = document.getElementById("tx-list");
    if (list) {
        list.querySelectorAll("li[data-id]").forEach((row) => formatRowMoney(row));
    }
    await updateSummaryUI();
});

export { saveTransaction };
