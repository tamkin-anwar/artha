// static/js/chart.js
import { onThemeChange, getCurrentTheme } from "./theme.js";
import { formatMoney } from "./currency.js";

export let financeChartInstance = null;
export let financeChartData = { income: 0, expense: 0 };

function getCSSVariable(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function getLegendColor() {
    const cssColor = getCSSVariable("--legend-color");
    if (cssColor) return cssColor;
    const theme = getCurrentTheme();
    return theme === "dark" ? "#fff" : "#000";
}

// Same role Finance's own donut charts already use this for (see
// fpRenderDonut's getVar("--bg-surface", ...) in finance.html): the
// surface-color gap between slices, not a contrasting ring.
function getSurfaceColor() {
    return getCSSVariable("--bg-surface") || (getCurrentTheme() === "dark" ? "#181d2a" : "#ffffff");
}

function syncCanvasSize(canvas) {
    if (!canvas) return;

    const rect = canvas.getBoundingClientRect();
    const cssWidth = Math.max(1, rect.width);
    const cssHeight = Math.max(1, rect.height);

    const dpr = window.devicePixelRatio || 1;
    const internalWidth = Math.max(1, Math.round(cssWidth * dpr));
    const internalHeight = Math.max(1, Math.round(cssHeight * dpr));

    canvas.style.width = `${cssWidth}px`;
    canvas.style.height = `${cssHeight}px`;

    if (canvas.width !== internalWidth) canvas.width = internalWidth;
    if (canvas.height !== internalHeight) canvas.height = internalHeight;
}

function prepare2dForDpr(canvas, ctx) {
    if (!canvas || !ctx) return null;

    const rect = canvas.getBoundingClientRect();
    const cssWidth = Math.max(1, rect.width);
    const cssHeight = Math.max(1, rect.height);
    const dpr = window.devicePixelRatio || 1;

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);

    return { cssWidth, cssHeight };
}

export function drawFallbackMessage(canvas, message) {
    if (!canvas?.getContext) return;

    syncCanvasSize(canvas);

    const ctx = canvas.getContext("2d");
    const dims = prepare2dForDpr(canvas, ctx);
    if (!dims) return;

    ctx.clearRect(0, 0, dims.cssWidth, dims.cssHeight);
    ctx.font = "14px Arial, sans-serif";
    ctx.fillStyle = getLegendColor();
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(message, dims.cssWidth / 2, dims.cssHeight / 2);
}

function getChartThemeOptions() {
    return { legendColor: getLegendColor() };
}

function makeMoneyNumber(value) {
    const num = Number(value);
    return Number.isFinite(num) ? num : 0;
}

function getBalance() {
    return makeMoneyNumber(financeChartData.income) - makeMoneyNumber(financeChartData.expense);
}

// No label/title args -> the resting state (the account's actual Balance).
// Passed a slice's own label/value on hover instead -- see the onHover
// callback below for why that replaces a native tooltip here.
function updateCenterLabel(label, value) {
    const titleEl = document.getElementById("chart-center-title");
    const valueEl = document.getElementById("chart-center-value");
    if (!titleEl || !valueEl) return;

    titleEl.textContent = label ?? "Balance";
    valueEl.textContent = value ?? formatMoney(getBalance());
}

export function initFinanceChart(ctx, income, expense) {
    const canvas = ctx?.canvas || ctx;

    if (!canvas || !canvas.getContext) {
        console.warn("Invalid canvas context provided to initFinanceChart.");
        return;
    }

    syncCanvasSize(canvas);

    if (typeof Chart === "undefined") {
        drawFallbackMessage(canvas, "Chart unavailable");
        return;
    }

    financeChartData = { income, expense };
    updateCenterLabel();

    // No on-slice value labels (unlike the Trend/Cash Flow bar charts) --
    // chartjs-plugin-datalabels centers them on the arc by default with
    // clip:false, and a money string like "$2,343.32" is routinely wider
    // than a two-value donut's ring is thick at that radius, so it spilled
    // out past the ring into the card background (reported as "broken").
    // The exact figures are already the stat tiles right above this card,
    // so the fix is removing the redundant label entirely, not repositioning
    // it -- legend + the center Balance figure already carry everything a
    // label would, matching Finance's own donuts (fpRenderDonut in
    // finance.html), which never had on-slice labels either.
    const incomeColor = getCSSVariable("--income-color") || "#10b981";
    const expenseColor = getCSSVariable("--expense-color") || "#ef4444";
    const { legendColor } = getChartThemeOptions();
    // Same role Finance's own donut charts already use this border for
    // (see fpRenderDonut's getVar("--bg-surface", ...) in finance.html):
    // a surface-color gap between slices, not a contrasting ring drawn
    // around them. This chart was still hardcoded to pure black, which
    // read as a heavy outline in light mode instead of the quiet gap it
    // was meant to be.
    const surfaceColor = getCSSVariable("--bg-surface") || (getCurrentTheme() === "dark" ? "#181d2a" : "#ffffff");

    if (financeChartInstance) {
        financeChartInstance.destroy();
        financeChartInstance = null;
    }

    financeChartInstance = new Chart(canvas, {
        type: "doughnut",
        data: {
            labels: ["Income", "Expense"],
            datasets: [
                {
                    data: [income, expense],
                    backgroundColor: [incomeColor, expenseColor],
                    borderColor: surfaceColor,
                    borderWidth: 2,
                    hoverOffset: 6,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            devicePixelRatio: window.devicePixelRatio || 1,
            animation: false,
            // Matches Finance's own donut charts (fpRenderDonut's
            // cutout:"68%" in finance.html) -- a thin ring reads as
            // considered, the same mark spec applied there.
            cutout: "68%",
            layout: {
                padding: 8,
            },
            plugins: {
                legend: {
                    labels: { color: legendColor },
                },
                // No native tooltip -- it draws directly on the canvas, and
                // Chart.js's own default positioning happily lands the
                // tooltip box right over the center Balance figure (an
                // absolutely-positioned DOM overlay one layer above the
                // canvas), painting both at once and making the number
                // "not show" behind it. onHover below swaps the center
                // figure itself to the hovered slice's label/value instead
                // -- same information, no second box competing for the
                // same spot in the hole.
                tooltip: { enabled: false },
            },
            onHover: (evt, elements, chart) => {
                evt.native.target.style.cursor = elements.length ? "pointer" : "default";
                if (elements.length) {
                    const idx = elements[0].index;
                    const label = chart.data.labels[idx];
                    const value = makeMoneyNumber(chart.data.datasets[0].data[idx]);
                    updateCenterLabel(label, formatMoney(value));
                } else {
                    updateCenterLabel();
                }
            },
        },
    });
}

export function updateFinanceChart(income, expense) {
    financeChartData = { income, expense };
    updateCenterLabel();

    const canvas = document.getElementById("financeChart");
    if (!canvas) return;

    syncCanvasSize(canvas);

    if (typeof Chart === "undefined") {
        drawFallbackMessage(canvas, "Chart unavailable");
        return;
    }

    if (!financeChartInstance) {
        initFinanceChart(canvas.getContext("2d"), income, expense);
        return;
    }

    financeChartInstance.data.datasets[0].data = [income, expense];
    financeChartInstance.data.datasets[0].borderColor = getSurfaceColor();
    financeChartInstance.options.devicePixelRatio = window.devicePixelRatio || 1;

    const { legendColor } = getChartThemeOptions();
    financeChartInstance.options.plugins.legend.labels.color = legendColor;

    financeChartInstance.update();
}

export function drawSpinner(canvas, frame = 0) {
    if (!canvas?.getContext) return;

    syncCanvasSize(canvas);

    const ctx = canvas.getContext("2d");
    const dims = prepare2dForDpr(canvas, ctx);
    if (!dims) return;

    ctx.clearRect(0, 0, dims.cssWidth, dims.cssHeight);

    const radius = Math.min(dims.cssWidth, dims.cssHeight) / 6;
    const centerX = dims.cssWidth / 2;
    const centerY = dims.cssHeight / 2;

    ctx.save();
    ctx.translate(centerX, centerY);
    ctx.rotate((frame * Math.PI) / 30);
    ctx.beginPath();
    ctx.arc(0, 0, radius, 0, Math.PI * 1.5);
    ctx.strokeStyle = getLegendColor();
    ctx.lineWidth = 4;
    ctx.stroke();
    ctx.restore();
}

let currentAbortController = null;

export async function updateChartData() {
    const canvas = document.getElementById("financeChart");
    if (!canvas) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    syncCanvasSize(canvas);

    if (typeof Chart === "undefined") {
        drawFallbackMessage(canvas, "Chart unavailable");
        return;
    }

    if (currentAbortController) currentAbortController.abort();
    currentAbortController = new AbortController();

    let frame = 0;
    const spinnerInterval = setInterval(() => drawSpinner(canvas, frame++), 50);

    try {
        const urlParams = new URLSearchParams(window.location.search);
        const scenarioId = urlParams.get("scenario_id");

        let endpoint = "/api/finance_totals";
        if (scenarioId) endpoint += `?scenario_id=${encodeURIComponent(scenarioId)}`;

        const res = await fetch(endpoint, {
            signal: currentAbortController.signal,
            headers: { "X-Requested-With": "XMLHttpRequest" },
        });

        if (!res.ok) throw new Error("Network response was not ok");

        const data = await res.json();
        if (typeof data.income !== "number" || typeof data.expense !== "number") {
            throw new Error("Invalid data format");
        }

        updateFinanceChart(data.income, data.expense);
    } catch (err) {
        if (err.name !== "AbortError") {
            console.error("Failed to update chart:", err);
            drawFallbackMessage(canvas, "Failed to load chart");
        }
    } finally {
        clearInterval(spinnerInterval);
    }
}

export function ensureChartIntegrity() {
    const canvas = document.getElementById("financeChart");
    if (canvas && !financeChartInstance) {
        initFinanceChart(canvas.getContext("2d"), financeChartData.income, financeChartData.expense);
    }
}

let resizeTimeout = null;
window.addEventListener("resize", () => {
    clearTimeout(resizeTimeout);
    resizeTimeout = setTimeout(() => {
        const canvas = document.getElementById("financeChart");
        if (!canvas) return;

        syncCanvasSize(canvas);

        if (financeChartInstance) {
            financeChartInstance.options.devicePixelRatio = window.devicePixelRatio || 1;
            financeChartInstance.resize();
            financeChartInstance.update();
        }

        updateCenterLabel();
    }, 150);
});

let themeUpdateTimeout = null;
onThemeChange(() => {
    clearTimeout(themeUpdateTimeout);
    themeUpdateTimeout = setTimeout(() => {
        if (!financeChartInstance) return;

        const { legendColor } = getChartThemeOptions();

        financeChartInstance.options.plugins.legend.labels.color = legendColor;
        // The slice border is a surface-color gap, not a fixed ring --
        // it has to follow the surface color across the light/dark
        // toggle the same way every other themed value here does.
        financeChartInstance.data.datasets[0].borderColor = getSurfaceColor();

        financeChartInstance.update();
        updateCenterLabel();
    }, 100);
});

document.addEventListener("currency-refresh-ui", () => {
    if (!financeChartInstance) return;

    financeChartInstance.update();
    updateCenterLabel();
});

export function getFinanceChartData() {
    return { ...financeChartData };
}