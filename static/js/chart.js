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

function safeRegisterDatalabels() {
    try {
        if (typeof Chart !== "undefined" && typeof ChartDataLabels !== "undefined") {
            if (!Chart.registry?.plugins?.get?.("datalabels")) {
                Chart.register(ChartDataLabels);
            }
        }
    } catch {
        // ignore
    }
}

function getChartThemeOptions() {
    const legendColor = getLegendColor();
    const bg = getCSSVariable("--bg-color") || (getCurrentTheme() === "dark" ? "#111827" : "#ffffff");
    const tooltipBg = getCSSVariable("--tooltip-bg") || bg;

    return { legendColor, tooltipBg };
}

function makeMoneyNumber(value) {
    const num = Number(value);
    return Number.isFinite(num) ? num : 0;
}

function getBalance() {
    return makeMoneyNumber(financeChartData.income) - makeMoneyNumber(financeChartData.expense);
}

function buildTooltipLabel(ctx) {
    const label = ctx.label ? `${ctx.label}: ` : "";
    const value = makeMoneyNumber(ctx.parsed);
    return `${label}${formatMoney(value)}`;
}

function buildDatalabel(value) {
    const num = makeMoneyNumber(value);
    return formatMoney(num);
}

function updateCenterLabel() {
    const titleEl = document.getElementById("chart-center-title");
    const valueEl = document.getElementById("chart-center-value");
    if (!titleEl || !valueEl) return;

    titleEl.textContent = "Balance";
    valueEl.textContent = formatMoney(getBalance());
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

    safeRegisterDatalabels();

    financeChartData = { income, expense };
    updateCenterLabel();

    const incomeColor = getCSSVariable("--income-color") || "#10b981";
    const expenseColor = getCSSVariable("--expense-color") || "#ef4444";
    const { legendColor, tooltipBg } = getChartThemeOptions();

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
                    borderColor: "#000000",
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
            cutout: "52%",
            layout: {
                padding: 8,
            },
            plugins: {
                legend: {
                    labels: { color: legendColor },
                },
                tooltip: {
                    backgroundColor: tooltipBg,
                    titleColor: legendColor,
                    bodyColor: legendColor,
                    callbacks: {
                        label: buildTooltipLabel,
                    },
                },
                datalabels: {
                    color: legendColor,
                    formatter: (value) => buildDatalabel(value),
                    font: {
                        size: 13,
                        weight: "700",
                        family: "Arial, sans-serif",
                    },
                    textStrokeColor: "rgba(0,0,0,0.15)",
                    textStrokeWidth: 0.5,
                },
            },
        },
        plugins: typeof ChartDataLabels !== "undefined" ? [ChartDataLabels] : [],
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
    financeChartInstance.data.datasets[0].borderColor = "#000000";
    financeChartInstance.options.devicePixelRatio = window.devicePixelRatio || 1;

    const { legendColor, tooltipBg } = getChartThemeOptions();
    financeChartInstance.options.plugins.legend.labels.color = legendColor;
    financeChartInstance.options.plugins.tooltip.titleColor = legendColor;
    financeChartInstance.options.plugins.tooltip.bodyColor = legendColor;
    financeChartInstance.options.plugins.tooltip.backgroundColor = tooltipBg;

    if (financeChartInstance.options.plugins.datalabels) {
        financeChartInstance.options.plugins.datalabels.color = legendColor;
        financeChartInstance.options.plugins.datalabels.formatter = (value) => buildDatalabel(value);
        financeChartInstance.options.plugins.datalabels.font = {
            size: 13,
            weight: "700",
            family: "Arial, sans-serif",
        };
        financeChartInstance.options.plugins.datalabels.textStrokeColor = "rgba(0,0,0,0.15)";
        financeChartInstance.options.plugins.datalabels.textStrokeWidth = 0.5;
    }

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

        const { legendColor, tooltipBg } = getChartThemeOptions();

        financeChartInstance.options.plugins.legend.labels.color = legendColor;
        financeChartInstance.options.plugins.tooltip.titleColor = legendColor;
        financeChartInstance.options.plugins.tooltip.bodyColor = legendColor;
        financeChartInstance.options.plugins.tooltip.backgroundColor = tooltipBg;

        if (financeChartInstance.options.plugins.datalabels) {
            financeChartInstance.options.plugins.datalabels.color = legendColor;
        }

        financeChartInstance.update();
        updateCenterLabel();
    }, 100);
});

document.addEventListener("currency-refresh-ui", () => {
    if (!financeChartInstance) return;

    if (financeChartInstance.options.plugins.datalabels) {
        financeChartInstance.options.plugins.datalabels.formatter = (value) => buildDatalabel(value);
        financeChartInstance.options.plugins.datalabels.font = {
            size: 13,
            weight: "700",
            family: "Arial, sans-serif",
        };
        financeChartInstance.options.plugins.datalabels.textStrokeColor = "rgba(0,0,0,0.15)";
        financeChartInstance.options.plugins.datalabels.textStrokeWidth = 0.5;
    }

    financeChartInstance.options.plugins.tooltip.callbacks = {
        label: buildTooltipLabel,
    };

    financeChartInstance.update();
    updateCenterLabel();
});

export function getFinanceChartData() {
    return { ...financeChartData };
}