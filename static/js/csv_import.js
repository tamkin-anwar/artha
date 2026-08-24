// static/js/csv_import.js — bank statement CSV import modal on /finance.
// No-ops entirely on any other page (the trigger button doesn't exist).
import { showToast } from "./toast.js";

function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
}

function csrfHeaders() {
    return { "X-CSRFToken": getCsrfToken() };
}

function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str == null ? "" : String(str);
    return div.innerHTML;
}

function initCsvImport() {
    const openBtn = document.getElementById("csv-import-btn");
    if (!openBtn) return; // not on /finance

    const backdrop = document.getElementById("csv-import-backdrop");
    const closeBtn = document.getElementById("csv-import-close");
    const fileInput = document.getElementById("csv-import-file");
    const passwordRow = document.getElementById("csv-import-password-row");
    const passwordInput = document.getElementById("csv-import-password");
    const previewBtn = document.getElementById("csv-import-preview-btn");
    const backBtn = document.getElementById("csv-import-back-btn");
    const commitBtn = document.getElementById("csv-import-commit-btn");
    const errorEl = document.getElementById("csv-import-error");
    const summaryEl = document.getElementById("csv-import-summary");
    const tbody = document.getElementById("csv-import-tbody");
    const stepUpload = document.getElementById("csv-import-step-upload");
    const stepPreview = document.getElementById("csv-import-step-preview");

    let categories = {};

    function resetToUpload() {
        stepPreview.hidden = true;
        stepUpload.hidden = false;
        errorEl.style.display = "none";
        errorEl.textContent = "";
        fileInput.value = "";
        tbody.innerHTML = "";
        previewBtn.disabled = false;
        previewBtn.textContent = "Preview";
        passwordRow.hidden = true;
        passwordInput.value = "";
    }

    function openModal() {
        resetToUpload();
        backdrop.hidden = false;
        document.addEventListener("keydown", onKeydown);
    }

    function closeModal() {
        backdrop.hidden = true;
        document.removeEventListener("keydown", onKeydown);
    }

    function onKeydown(e) {
        if (e.key === "Escape") closeModal();
    }

    openBtn.addEventListener("click", openModal);
    closeBtn.addEventListener("click", closeModal);
    backdrop.addEventListener("mousedown", (e) => {
        if (e.target === backdrop) closeModal();
    });
    backBtn.addEventListener("click", resetToUpload);

    function categoryOptionsHtml(selected) {
        let html = `<option value="">Uncategorized</option>`;
        for (const [key, label] of Object.entries(categories)) {
            html += `<option value="${key}" ${key === selected ? "selected" : ""}>${escapeHtml(label)}</option>`;
        }
        return html;
    }

    function renderRows(rows) {
        tbody.innerHTML = rows.map((row, i) => {
            const dupe = row.duplicate;
            return `
                <tr class="${dupe ? "csv-import-row-dupe" : ""}" data-index="${i}">
                    <td><input type="checkbox" class="csv-import-row-check" ${dupe ? "" : "checked"}></td>
                    <td>${escapeHtml(row.date)}</td>
                    <td class="csv-import-desc">${escapeHtml(row.description)}${dupe ? ' <span class="csv-import-badge">already imported</span>' : ""}</td>
                    <td style="text-align:right; font-family:'JetBrains Mono',monospace;">${Number(row.amount).toFixed(2)}</td>
                    <td>
                        <select class="csv-import-row-type">
                            <option value="expense" ${row.type === "expense" ? "selected" : ""}>Expense</option>
                            <option value="income" ${row.type === "income" ? "selected" : ""}>Income</option>
                        </select>
                    </td>
                    <td>
                        <select class="csv-import-row-category">${categoryOptionsHtml(row.category)}</select>
                    </td>
                </tr>
            `;
        }).join("");
    }

    let parsedRows = [];

    previewBtn.addEventListener("click", async () => {
        const file = fileInput.files && fileInput.files[0];
        if (!file) {
            errorEl.textContent = "Choose a CSV file first.";
            errorEl.style.display = "";
            return;
        }

        errorEl.style.display = "none";
        previewBtn.disabled = true;
        previewBtn.textContent = "Reading…";

        const formData = new FormData();
        formData.append("statement", file);
        if (passwordInput.value) formData.append("pdf_password", passwordInput.value);

        try {
            const res = await fetch("/finance/import/preview", {
                method: "POST",
                credentials: "same-origin",
                headers: csrfHeaders(),
                body: formData,
            });
            const data = await res.json().catch(() => ({}));

            if (!res.ok) {
                errorEl.textContent = data.message || "Could not read that file.";
                errorEl.style.display = "";
                previewBtn.disabled = false;
                previewBtn.textContent = "Preview";
                if (data.needs_password) {
                    passwordRow.hidden = false;
                    passwordInput.focus();
                }
                return;
            }

            parsedRows = data.rows || [];
            categories = data.categories || {};

            const dupeCount = parsedRows.filter((r) => r.duplicate).length;
            const warningCount = (data.warnings || []).length;
            let summary = `<strong>${parsedRows.length}</strong> transaction${parsedRows.length === 1 ? "" : "s"} found`;
            if (dupeCount) summary += ` · <strong>${dupeCount}</strong> look already imported (unchecked below)`;
            if (warningCount) summary += ` · ${warningCount} row${warningCount === 1 ? "" : "s"} skipped (couldn't be read)`;
            summaryEl.innerHTML = summary;

            renderRows(parsedRows);
            stepUpload.hidden = true;
            stepPreview.hidden = false;
        } catch (err) {
            errorEl.textContent = "Something went wrong reading that file.";
            errorEl.style.display = "";
            previewBtn.disabled = false;
            previewBtn.textContent = "Preview";
        }
    });

    commitBtn.addEventListener("click", async () => {
        const rowEls = Array.from(tbody.querySelectorAll("tr"));
        const rows = [];
        rowEls.forEach((tr) => {
            const check = tr.querySelector(".csv-import-row-check");
            if (!check || !check.checked) return;
            const idx = Number(tr.dataset.index);
            const base = parsedRows[idx];
            const type = tr.querySelector(".csv-import-row-type").value;
            const category = tr.querySelector(".csv-import-row-category").value;
            rows.push({ ...base, type, category });
        });

        if (!rows.length) {
            showToast("No rows selected to import", "info");
            return;
        }

        commitBtn.disabled = true;
        commitBtn.textContent = "Importing…";

        try {
            const res = await fetch("/finance/import/commit", {
                method: "POST",
                credentials: "same-origin",
                headers: { "Content-Type": "application/json", ...csrfHeaders() },
                body: JSON.stringify({ rows }),
            });
            const data = await res.json().catch(() => ({}));

            if (!res.ok) {
                showToast(data.message || "Import failed", "error");
                commitBtn.disabled = false;
                commitBtn.textContent = "Import selected";
                return;
            }

            showToast(data.message || "Import complete", "success");
            // Simplest correct way to reflect new totals/chart/budget status
            // everywhere on the page rather than hand-patching every stat.
            window.location.reload();
        } catch (err) {
            showToast("Import failed", "error");
            commitBtn.disabled = false;
            commitBtn.textContent = "Import selected";
        }
    });
}

document.addEventListener("DOMContentLoaded", initCsvImport);
