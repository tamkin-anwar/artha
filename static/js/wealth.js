// static/js/wealth.js: the redesigned Wealth page (templates/accounts.html).
// Draws net worth over time from the same history the app's Wealth tab
// uses (one point per day a balance changed, plus today), scrubbable with
// a pointer or the arrow keys.

import { formatMoneyIn } from "./currency.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const W = 640;
const TOP = 18;
const BOTTOM = 214;

function el(name, attrs) {
    const node = document.createElementNS(SVG_NS, name);
    Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
    return node;
}

function dayNumber(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return Date.UTC(y, m - 1, d) / 86400000;
}

function shortDate(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function setupChart() {
    const figure = document.getElementById("worth-chart");
    if (!figure) return;
    const svg = figure.querySelector("svg");
    const readout = figure.querySelector("[data-pace-readout]");
    const currency = figure.dataset.currency;
    let history = [];
    try { history = JSON.parse(figure.dataset.series || "[]"); } catch (e) {}
    if (history.length < 2) return;

    const points = history.map((p) => ({ date: p.date, t: dayNumber(p.date), v: Number(p.net_worth) }));
    const t0 = points[0].t;
    const t1 = Math.max(points[points.length - 1].t, t0 + 1);
    const values = points.map((p) => p.v);
    let lo = Math.min(...values);
    let hi = Math.max(...values);
    const pad = Math.max((hi - lo) * 0.12, Math.abs(hi) * 0.02, 1);
    lo -= pad;
    hi += pad;
    const x = (t) => ((t - t0) / (t1 - t0)) * W;
    const y = (v) => BOTTOM - ((v - lo) / (hi - lo)) * (BOTTOM - TOP);
    const money = (v) => formatMoneyIn(v, currency, { wholeUnits: true });

    const defs = el("defs", {});
    const gradient = el("linearGradient", { id: "worth-fill", x1: "0", y1: "0", x2: "0", y2: "1" });
    gradient.append(
        el("stop", { offset: "0", "stop-color": "var(--accent)", "stop-opacity": "0.28" }),
        el("stop", { offset: "1", "stop-color": "var(--accent)", "stop-opacity": "0" }),
    );
    defs.append(gradient);
    svg.append(defs);

    // A balance holds until the next change, so the line steps.
    let d = `M${x(points[0].t).toFixed(1)} ${y(points[0].v).toFixed(1)}`;
    for (let i = 1; i < points.length; i++) {
        d += ` L${x(points[i].t).toFixed(1)} ${y(points[i - 1].v).toFixed(1)} L${x(points[i].t).toFixed(1)} ${y(points[i].v).toFixed(1)}`;
    }
    svg.append(el("line", { x1: 0, y1: BOTTOM, x2: W, y2: BOTTOM, stroke: "var(--hairline)", "stroke-width": 1 }));
    svg.append(el("path", { d: `${d} L${W} ${BOTTOM} L0 ${BOTTOM} Z`, fill: "url(#worth-fill)" }));
    svg.append(el("path", { d, stroke: "var(--accent)", "stroke-width": 2.5, fill: "none", "stroke-linejoin": "round", "stroke-linecap": "round" }));
    const last = points[points.length - 1];
    const rule = el("line", { x1: x(last.t), y1: TOP, x2: x(last.t), y2: BOTTOM, stroke: "var(--text-primary)", "stroke-opacity": 0.22 });
    const halo = el("circle", { cx: x(last.t), cy: y(last.v), r: 9, fill: "var(--bg-page)" });
    const dot = el("circle", { cx: x(last.t), cy: y(last.v), r: 6, fill: "var(--accent)" });
    svg.append(rule, halo, dot);

    const todayText = readout.textContent;
    let index = points.length - 1;

    function moveTo(i) {
        index = Math.max(0, Math.min(points.length - 1, i));
        const p = points[index];
        rule.setAttribute("x1", x(p.t));
        rule.setAttribute("x2", x(p.t));
        [halo, dot].forEach((n) => { n.setAttribute("cx", x(p.t)); n.setAttribute("cy", y(p.v)); });
        readout.textContent = index === points.length - 1 ? todayText : `${shortDate(p.date)}: ${money(p.v)}`;
    }

    svg.addEventListener("pointermove", (e) => {
        const box = svg.getBoundingClientRect();
        const t = t0 + ((e.clientX - box.left) / box.width) * (t1 - t0);
        let nearest = 0;
        points.forEach((p, i) => { if (p.t <= t) nearest = i; });
        moveTo(nearest);
    });
    svg.addEventListener("pointerleave", () => moveTo(points.length - 1));

    figure.tabIndex = 0;
    readout.setAttribute("aria-live", "polite");
    figure.addEventListener("keydown", (e) => {
        if (e.key === "ArrowLeft") { e.preventDefault(); moveTo(index - 1); }
        if (e.key === "ArrowRight") { e.preventDefault(); moveTo(index + 1); }
        if (e.key === "Home") { e.preventDefault(); moveTo(0); }
        if (e.key === "End" || e.key === "Escape") { e.preventDefault(); moveTo(points.length - 1); }
    });
    figure.addEventListener("blur", () => moveTo(points.length - 1));
}

setupChart();
