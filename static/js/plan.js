// static/js/plan.js: the redesigned Plan page (templates/plan.html).
// Setting the budget and category limits posts to the same routes as the
// Reports page's forms (finance.routes' save_budget, save_category_budget,
// remove_category_budget), then reloads, since every figure on the page
// is computed server-side.

import { csrfToken, postJSON, toastAfterReload } from "./shell.js";
import { formatMoneyIn } from "./currency.js";
import { showToast } from "./toast.js";

const plan = document.getElementById("plan");
const currency = plan?.dataset.currency || "USD";

function cleanAmount(raw) {
    return (raw || "").trim().replace(/[^\d.,]/g, "").replace(/,(?=\d{3}\b)/g, "").replace(",", ".");
}

async function postForm(url, fields) {
    const data = new FormData();
    data.append("csrf_token", csrfToken());
    Object.entries(fields).forEach(([k, v]) => data.append(k, v));
    const res = await fetch(url, {
        method: "POST",
        headers: { "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": csrfToken() },
        body: data,
    });
    let body = {};
    try { body = await res.json(); } catch (e) {}
    return { ok: res.ok, message: body.message };
}

function showError(form, message) {
    const el = form.querySelector(".sheet-error");
    el.textContent = message;
    el.hidden = false;
}

async function submitting(form, work) {
    const buttons = form.querySelectorAll("button");
    buttons.forEach((b) => { b.disabled = true; });
    try {
        await work();
    } catch (err) {
        showError(form, "You're offline. Check your connection and try again.");
    } finally {
        buttons.forEach((b) => { b.disabled = false; });
    }
}

function toggle(button, form) {
    const opening = form.hidden;
    form.hidden = !opening;
    button?.setAttribute("aria-expanded", String(opening));
    if (opening) form.querySelector("input, select")?.focus();
}

// The monthly budget.
const budgetForm = document.getElementById("budget-form");
const editBudget = document.querySelector("[data-edit-budget]");
editBudget?.addEventListener("click", () => toggle(editBudget, budgetForm));
budgetForm?.addEventListener("submit", (e) => {
    e.preventDefault();
    const input = budgetForm.elements.monthly_cap;
    const amount = cleanAmount(input.value) || cleanAmount(input.placeholder);
    if (!amount || Number(amount) <= 0) {
        showError(budgetForm, "Enter a monthly amount above zero.");
        input.focus();
        return;
    }
    submitting(budgetForm, async () => {
        const res = await postForm("/finance/budget", { monthly_cap: amount });
        if (!res.ok) return showError(budgetForm, res.message || "Artha couldn't save your budget.");
        toastAfterReload("Budget saved");
        window.location.reload();
    });
});
budgetForm?.querySelector("[data-clear-budget]")?.addEventListener("click", () => {
    if (!window.confirm("Remove your monthly budget? Safe to spend goes away until you set one again.")) return;
    submitting(budgetForm, async () => {
        const res = await postForm("/finance/budget", { monthly_cap: "" });
        if (!res.ok) return showError(budgetForm, res.message || "Artha couldn't remove your budget.");
        toastAfterReload("Budget removed");
        window.location.reload();
    });
});

// Adding a category limit, suggesting what this person usually spends.
const limitForm = document.getElementById("limit-form");
const addLimit = document.querySelector("[data-add-limit]");
addLimit?.addEventListener("click", () => toggle(addLimit, limitForm));
function suggest() {
    if (!limitForm) return;
    const option = limitForm.elements.category.selectedOptions[0];
    const typical = option?.dataset.typical;
    const hint = limitForm.querySelector("[data-limit-hint]");
    const input = limitForm.elements.monthly_cap;
    if (typical && Number(typical) > 0) {
        hint.textContent = `You usually spend ${formatMoneyIn(typical, currency, { wholeUnits: true })} a month on ${option.textContent.trim().toLowerCase()}.`;
        input.placeholder = String(Math.max(10, Math.round(Number(typical) / 10) * 10));
    } else {
        hint.textContent = "";
        input.placeholder = "0";
    }
}
limitForm?.elements.category.addEventListener("change", suggest);
suggest();
limitForm?.addEventListener("submit", (e) => {
    e.preventDefault();
    const input = limitForm.elements.monthly_cap;
    const amount = cleanAmount(input.value) || (input.placeholder !== "0" ? input.placeholder : "");
    if (!amount || Number(amount) <= 0) {
        showError(limitForm, "Enter a monthly limit above zero.");
        input.focus();
        return;
    }
    submitting(limitForm, async () => {
        const res = await postForm("/finance/category-budget", { category: limitForm.elements.category.value, monthly_cap: amount });
        if (!res.ok) return showError(limitForm, res.message || "Artha couldn't save that limit.");
        toastAfterReload("Limit added");
        window.location.reload();
    });
});

// Changing or removing an existing limit.
document.querySelectorAll(".limit-row").forEach((row) => {
    const form = row.querySelector(".limit-edit");
    const edit = row.querySelector("[data-edit-limit]");
    edit.addEventListener("click", () => toggle(edit, form));
    form.addEventListener("submit", (e) => {
        e.preventDefault();
        const amount = cleanAmount(form.elements.monthly_cap.value);
        if (!amount || Number(amount) <= 0) {
            showError(form, "Enter a monthly limit above zero.");
            return;
        }
        submitting(form, async () => {
            const res = await postForm("/finance/category-budget", {
                category: row.dataset.category,
                monthly_cap: amount,
                rollover: form.elements.rollover.checked ? "1" : "0",
            });
            if (!res.ok) return showError(form, res.message || "Artha couldn't save that limit.");
            toastAfterReload("Limit saved");
            window.location.reload();
        });
    });
    form.querySelector("[data-remove-limit]").addEventListener("click", () => {
        submitting(form, async () => {
            const res = await postForm(`/finance/category-budget/${encodeURIComponent(row.dataset.category)}/delete`, {});
            if (!res.ok) return showError(form, res.message || "Artha couldn't remove that limit.");
            toastAfterReload("Limit removed");
            window.location.reload();
        });
    });
});

// Charges that look like bills (services/recurring_detect.py).
document.querySelectorAll("[data-suggestion]").forEach((row) => {
    const key = row.dataset.suggestion;
    const act = async (url, message) => {
        row.querySelectorAll("button").forEach((b) => { b.disabled = true; });
        try {
            const res = await postJSON(url, { key });
            if (!res.ok) throw new Error(res.data.message || "failed");
            toastAfterReload(message || res.data.message);
            window.location.reload();
        } catch (err) {
            row.querySelectorAll("button").forEach((b) => { b.disabled = false; });
            showToast("Artha couldn't save that. Try again in a moment.", "error");
        }
    };
    row.querySelector("[data-track-bill]").addEventListener("click", () => act("/plan/bills/track"));
    row.querySelector("[data-dismiss-bill]").addEventListener("click", () => act("/plan/bills/dismiss", "Won't suggest it again"));
});

// Savings goals (services/goals.py).
async function goalRequest(form, url, body, message) {
    await submitting(form, async () => {
        const res = await postJSON(url, body);
        if (!res.ok) return showError(form, res.data.message || "Artha couldn't save that.");
        toastAfterReload(message);
        window.location.reload();
    });
}

const goalForm = document.getElementById("goal-form");
const addGoal = document.querySelector("[data-add-goal]");
addGoal?.addEventListener("click", () => toggle(addGoal, goalForm));
goalForm?.addEventListener("submit", (e) => {
    e.preventDefault();
    const f = goalForm.elements;
    if (!f.name.value.trim()) return showError(goalForm, "Give the goal a name.");
    const target = cleanAmount(f.target_amount.value);
    if (!target || Number(target) <= 0) return showError(goalForm, "Enter how much the goal is.");
    goalRequest(goalForm, "/plan/goals", { name: f.name.value.trim(), target_amount: target, target_date: f.target_date.value }, "Goal added");
});

document.querySelectorAll("[data-goal]").forEach((row) => {
    const id = row.dataset.goal;
    const money = row.querySelector(".goal-money");
    const edit = row.querySelector(".goal-edit");
    const addMoney = row.querySelector("[data-add-money]");
    addMoney?.addEventListener("click", () => { edit.hidden = true; toggle(addMoney, money); });
    row.querySelector("[data-edit-goal]").addEventListener("click", (e) => { money.hidden = true; toggle(e.currentTarget, edit); });

    const move = (sign) => {
        const raw = cleanAmount(money.elements.amount.value) || cleanAmount(money.elements.amount.placeholder);
        if (!raw || Number(raw) <= 0) return showError(money, "Enter an amount.");
        goalRequest(money, `/plan/goals/${id}/add`, { amount: sign * Number(raw) }, sign > 0 ? "Added" : "Taken out");
    };
    money.addEventListener("submit", (e) => { e.preventDefault(); move(1); });
    money.querySelector("[data-take-out]").addEventListener("click", () => move(-1));

    edit.addEventListener("submit", (e) => {
        e.preventDefault();
        const f = edit.elements;
        const target = cleanAmount(f.target_amount.value);
        if (!f.name.value.trim()) return showError(edit, "Give the goal a name.");
        if (!target || Number(target) <= 0) return showError(edit, "Enter how much the goal is.");
        goalRequest(edit, `/plan/goals/${id}`, { name: f.name.value.trim(), target_amount: target, target_date: f.target_date.value }, "Goal saved");
    });
    edit.querySelector("[data-delete-goal]").addEventListener("click", () => {
        if (!window.confirm("Delete this goal? What you've saved toward it is only a note here, so no money moves.")) return;
        goalRequest(edit, `/plan/goals/${id}/delete`, {}, "Goal deleted");
    });
});
