// static/js/scenarios.js
import { formatMoney } from "./currency.js";

function applyMoneyFormatting() {
    document.querySelectorAll(".scenario-money[data-money-value]").forEach((el) => {
        const num = parseFloat(el.getAttribute("data-money-value"));
        if (!Number.isFinite(num)) return;

        const signed = el.getAttribute("data-money-signed") === "true";
        const suffix = el.getAttribute("data-money-suffix") || "";
        const formatted = formatMoney(Math.abs(num));

        el.textContent = (signed ? (num >= 0 ? "+" : "-") + formatted : formatted) + suffix;
    });
}

function initScaleLabels() {
    document.querySelectorAll('input[type="range"][id]').forEach((input) => {
        const label = document.querySelector(`.scenario-scale-value[data-for="${input.id}"]`);
        if (!label) return;
        input.addEventListener("input", () => {
            label.textContent = input.value;
        });
    });
}

function initDeleteConfirm() {
    document.querySelectorAll(".scenario-delete-form").forEach((form) => {
        form.addEventListener("submit", (e) => {
            if (!window.confirm("Delete this scenario? This can't be undone.")) {
                e.preventDefault();
            }
        });
    });
}

const VERDICT_LABEL = { do_it: "Do it", bad_idea: "Bad idea", wait: "Wait" };
const VERDICT_CLASS = { do_it: "badge-do-it", bad_idea: "badge-bad", wait: "badge-wait" };
const RISK_CLASS = { low: "badge-do-it", medium: "badge-wait", high: "badge-bad" };
const MAX_COMPARE = 4;

function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str == null ? "" : String(str);
    return div.innerHTML;
}

function signedMoney(value) {
    return (value < 0 ? "-" : "+") + formatMoney(Math.abs(value));
}

function initCompareModal() {
    const dataScript = document.getElementById("scenario-compare-data");
    const openBtn = document.getElementById("compare-open-btn");
    const overlay = document.getElementById("compare-modal-overlay");
    const closeBtn = document.getElementById("compare-modal-close");
    const body = document.getElementById("compare-modal-body");
    if (!dataScript || !openBtn || !overlay || !closeBtn || !body) return;

    let scenarios;
    try {
        scenarios = JSON.parse(dataScript.textContent);
    } catch {
        return;
    }

    function renderPicker() {
        const rows = scenarios
            .map(
                (s) => `
                <label style="display:flex; align-items:center; gap:10px; padding:10px 12px; border:1px solid var(--border-subtle); border-radius:10px; cursor:pointer; margin-bottom:8px;">
                    <input type="checkbox" class="compare-pick" value="${s.id}" style="width:16px; height:16px; accent-color:var(--gold); flex-shrink:0;">
                    <span style="flex:1; min-width:0;">
                        <span style="display:block; font-size:14px; font-weight:600; color:var(--text-primary);">${escapeHtml(s.title)}</span>
                        <span class="eyebrow" style="font-size:10px;">${escapeHtml(s.category)}</span>
                    </span>
                    <span class="${VERDICT_CLASS[s.verdict_label] || "badge-wait"}" style="font-size:11px; padding:2px 10px; flex-shrink:0;">${VERDICT_LABEL[s.verdict_label] || "Wait"}</span>
                </label>`
            )
            .join("");

        body.innerHTML = `
            <p style="font-size:13px; color:var(--text-secondary); margin:0 0 12px;">Pick 2-${MAX_COMPARE} scenarios to see their verdicts side by side.</p>
            <div>${rows}</div>
            <button type="button" id="compare-run-btn" class="btn-primary" disabled style="margin-top:4px; width:100%; padding:10px; font-size:14px; opacity:0.5; cursor:default;">Compare</button>
        `;

        const checks = [...body.querySelectorAll(".compare-pick")];
        const runBtn = document.getElementById("compare-run-btn");

        checks.forEach((cb) => {
            cb.addEventListener("change", () => {
                const checked = checks.filter((c) => c.checked);
                if (checked.length > MAX_COMPARE) {
                    cb.checked = false;
                    return;
                }
                runBtn.disabled = checked.length < 2;
                runBtn.style.opacity = checked.length < 2 ? "0.5" : "1";
                runBtn.style.cursor = checked.length < 2 ? "default" : "pointer";
            });
        });

        runBtn.addEventListener("click", () => {
            const ids = checks.filter((c) => c.checked).map((c) => Number(c.value));
            renderComparison(ids);
        });
    }

    function renderComparison(ids) {
        const picked = scenarios.filter((s) => ids.includes(s.id));
        const cols = picked
            .map(
                (s) => `
                <div style="min-width:200px; flex:1; border:1px solid var(--border-subtle); border-radius:12px; padding:16px; background:var(--bg-card);">
                    <p class="eyebrow" style="margin-bottom:4px;">${escapeHtml(s.category)}</p>
                    <h3 style="font-family:'Fraunces',serif; font-size:17px; font-weight:600; color:var(--text-primary); margin:0 0 10px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escapeHtml(s.title)}</h3>
                    <div style="display:flex; gap:6px; margin-bottom:12px; flex-wrap:wrap;">
                        <span class="${VERDICT_CLASS[s.verdict_label] || "badge-wait"}" style="font-size:11px; padding:3px 10px;">${VERDICT_LABEL[s.verdict_label] || "Wait"}</span>
                        <span class="${RISK_CLASS[s.risk_level] || "badge-wait"} capitalize" style="font-size:11px; padding:3px 10px;">${escapeHtml(s.risk_level)} risk</span>
                    </div>
                    <p style="font-size:11px; color:var(--text-muted); margin:0 0 8px;">${escapeHtml(s.month_label)}${s.projected ? " (projected)" : ""}</p>
                    <p style="font-size:11px; color:var(--text-muted); margin:0 0 2px;">Net, without this</p>
                    <p class="scenario-money" style="font-size:16px; font-weight:600; color:${s.net_before >= 0 ? "var(--emerald)" : "var(--red)"}; margin:0 0 8px;">${signedMoney(s.net_before)}</p>
                    <p style="font-size:11px; color:var(--text-muted); margin:0 0 2px;">Net, with this scenario</p>
                    <p class="scenario-money" style="font-size:16px; font-weight:700; color:${s.net_after >= 0 ? "var(--emerald)" : "var(--red)"}; margin:0 0 10px;">${signedMoney(s.net_after)}</p>
                    <p style="font-size:12px; color:var(--text-secondary); line-height:1.5;">${escapeHtml(s.insight)}</p>
                </div>`
            )
            .join("");

        body.innerHTML = `
            <button type="button" id="compare-back-btn" style="background:none; border:none; color:var(--gold); font-size:13px; cursor:pointer; padding:0; margin-bottom:14px;">&larr; Change selection</button>
            <div style="display:flex; gap:12px; overflow-x:auto; padding-bottom:4px;">${cols}</div>
        `;
        document.getElementById("compare-back-btn").addEventListener("click", renderPicker);
    }

    function openModal() {
        renderPicker();
        overlay.style.display = "flex";
        overlay.classList.remove("hidden");
    }

    function closeModal() {
        overlay.classList.add("hidden");
        overlay.style.display = "none";
    }

    openBtn.addEventListener("click", openModal);
    closeBtn.addEventListener("click", closeModal);
    overlay.addEventListener("click", (e) => {
        if (e.target === overlay) closeModal();
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !overlay.classList.contains("hidden")) closeModal();
    });
}

function initScenarios() {
    applyMoneyFormatting();
    initScaleLabels();
    initDeleteConfirm();
    initCompareModal();
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initScenarios);
} else {
    initScenarios();
}

document.addEventListener("currency-refresh-ui", applyMoneyFormatting);
