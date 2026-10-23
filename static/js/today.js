// static/js/today.js: the redesigned Today page (templates/index.html).
// Two things: the hero figure's switcher (Safe to spend, spent this
// month, net worth; remembered per browser, like the app's), and the
// pace chart, drawn from the same spending_by_day the app's chart uses
// and scrubbable with a pointer or the arrow keys.

import { formatMoneyIn } from "./currency.js";
import { turnOnPushHere } from "./notifications.js";
import { postJSON } from "./shell.js";
import { showToast } from "./toast.js";

const HERO_KEY = "artha-today-hero";

function setupHero() {
    const hero = document.getElementById("hero");
    const button = document.getElementById("hero-switch");
    const menu = document.getElementById("hero-menu");
    const label = document.getElementById("hero-switch-label");
    if (!hero || !button || !menu) return;

    const choices = [...menu.querySelectorAll("[data-hero-choice]")];
    const available = choices.map((c) => c.dataset.heroChoice);

    function show(choice) {
        if (!available.includes(choice)) choice = hero.dataset.default;
        hero.querySelectorAll("[data-hero-view]").forEach((view) => {
            view.hidden = view.dataset.heroView !== choice;
        });
        choices.forEach((c) => c.setAttribute("aria-checked", String(c.dataset.heroChoice === choice)));
        const picked = choices.find((c) => c.dataset.heroChoice === choice);
        if (picked) label.textContent = picked.dataset.label;
    }

    function close() {
        menu.hidden = true;
        button.setAttribute("aria-expanded", "false");
    }

    let stored = null;
    try { stored = localStorage.getItem(HERO_KEY); } catch (e) {}
    show(stored || hero.dataset.default);

    // With only one figure to show, there's nothing to switch to.
    if (choices.length < 2) {
        button.disabled = true;
        button.querySelector("svg")?.remove();
        return;
    }

    button.addEventListener("click", () => {
        const opening = menu.hidden;
        menu.hidden = !opening;
        button.setAttribute("aria-expanded", String(opening));
        if (opening) (menu.querySelector('[aria-checked="true"]') || choices[0]).focus();
    });
    choices.forEach((choice, index) => {
        choice.addEventListener("click", () => {
            try { localStorage.setItem(HERO_KEY, choice.dataset.heroChoice); } catch (e) {}
            show(choice.dataset.heroChoice);
            close();
            button.focus();
        });
        choice.addEventListener("keydown", (e) => {
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                e.preventDefault();
                const next = (index + (e.key === "ArrowDown" ? 1 : -1) + choices.length) % choices.length;
                choices[next].focus();
            }
        });
    });
    document.addEventListener("click", (e) => {
        if (!menu.hidden && !hero.contains(e.target)) close();
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !menu.hidden) {
            close();
            button.focus();
        }
    });
}

const SVG_NS = "http://www.w3.org/2000/svg";
const W = 640;
const H = 230;
const TOP = 18;
const BOTTOM = 214;

function el(name, attrs) {
    const node = document.createElementNS(SVG_NS, name);
    Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
    return node;
}

function setupPace() {
    const figure = document.getElementById("pace");
    if (!figure) return;
    const svg = figure.querySelector("svg");
    const readout = figure.querySelector("[data-pace-readout]");
    const currency = figure.dataset.currency;
    const days = Number(figure.dataset.days) || 30;
    const cap = figure.dataset.cap ? Number(figure.dataset.cap) : null;
    const month = figure.dataset.month;
    let series = [];
    try { series = JSON.parse(figure.dataset.series || "[]"); } catch (e) {}
    const points = [{ day: 0, spent: 0 }, ...series.map((p) => ({ day: p.day, spent: Number(p.spent) }))];
    const today = points[points.length - 1];

    const top = Math.max(cap || 0, ...points.map((p) => p.spent), 1) * 1.08;
    const x = (day) => (day / days) * W;
    const y = (value) => BOTTOM - (value / top) * (BOTTOM - TOP);
    const money = (v) => formatMoneyIn(v, currency, { wholeUnits: true });

    const defs = el("defs", {});
    const gradient = el("linearGradient", { id: "pace-fill", x1: "0", y1: "0", x2: "0", y2: "1" });
    gradient.append(
        el("stop", { offset: "0", "stop-color": "var(--accent)", "stop-opacity": "0.3" }),
        el("stop", { offset: "1", "stop-color": "var(--accent)", "stop-opacity": "0" }),
    );
    defs.append(gradient);
    svg.append(defs);

    svg.append(el("line", { x1: 0, y1: BOTTOM, x2: W, y2: BOTTOM, stroke: "var(--hairline)", "stroke-width": 1 }));
    if (cap) {
        // An even pace: spending the budget evenly from the 1st to the last day.
        svg.append(el("path", {
            d: `M0 ${y(0)} L${W} ${y(cap)}`,
            stroke: "var(--text-secondary)", "stroke-opacity": 0.6, "stroke-width": 1.5,
            "stroke-dasharray": "3 5", fill: "none",
        }));
    }
    const line = points.map((p, i) => `${i ? "L" : "M"}${x(p.day).toFixed(1)} ${y(p.spent).toFixed(1)}`).join(" ");
    svg.append(el("path", { d: `${line} L${x(today.day).toFixed(1)} ${BOTTOM} L0 ${BOTTOM} Z`, fill: "url(#pace-fill)" }));
    svg.append(el("path", {
        d: line, stroke: "var(--accent)", "stroke-width": 2.5, fill: "none",
        "stroke-linejoin": "round", "stroke-linecap": "round",
    }));
    const rule = el("line", { x1: x(today.day), y1: TOP, x2: x(today.day), y2: BOTTOM, stroke: "var(--text-primary)", "stroke-opacity": 0.22 });
    const halo = el("circle", { cx: x(today.day), cy: y(today.spent), r: 9, fill: "var(--bg-page)" });
    const dot = el("circle", { cx: x(today.day), cy: y(today.spent), r: 6, fill: "var(--accent)" });
    svg.append(rule, halo, dot);

    if (points.length < 2) return;

    const todayText = readout.textContent;
    let current = today.day;

    function moveTo(day) {
        current = Math.max(1, Math.min(today.day, day));
        const p = points[current];
        [rule].forEach((n) => { n.setAttribute("x1", x(p.day)); n.setAttribute("x2", x(p.day)); });
        [halo, dot].forEach((n) => { n.setAttribute("cx", x(p.day)); n.setAttribute("cy", y(p.spent)); });
        readout.textContent = p.day === today.day
            ? todayText
            : `${month} ${p.day}: ${money(p.spent)} spent` + (cap ? `, even pace ${money((cap * p.day) / days)}` : "");
    }

    function reset() { moveTo(today.day); }

    svg.addEventListener("pointermove", (e) => {
        const box = svg.getBoundingClientRect();
        const day = Math.round(((e.clientX - box.left) / box.width) * days);
        moveTo(day);
    });
    svg.addEventListener("pointerleave", reset);

    // Keyboard: the chart is focusable, and the arrow keys walk the days.
    figure.tabIndex = 0;
    figure.setAttribute("aria-describedby", "pace-hint");
    const hint = document.createElement("span");
    hint.id = "pace-hint";
    hint.className = "sr-only";
    hint.textContent = "Use the left and right arrow keys to see each day.";
    figure.append(hint);
    readout.setAttribute("aria-live", "polite");
    figure.addEventListener("keydown", (e) => {
        if (e.key === "ArrowLeft") { e.preventDefault(); moveTo(current - 1); }
        if (e.key === "ArrowRight") { e.preventDefault(); moveTo(current + 1); }
        if (e.key === "Home") { e.preventDefault(); moveTo(1); }
        if (e.key === "End" || e.key === "Escape") { e.preventDefault(); reset(); }
    });
    figure.addEventListener("blur", reset);
}

setupHero();
setupPace();

// The one-time pace heads-up card. Either answer is remembered on the
// account, so it never shows again on any device. "Turn on" also asks
// this browser for notifications, since that's where it would arrive.
function setupPacePrompt() {
    const card = document.getElementById("pace-prompt");
    if (!card) return;
    card.querySelectorAll("[data-pace-answer]").forEach((button) => {
        button.addEventListener("click", async () => {
            const on = button.dataset.paceAnswer === "on";
            card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
            const saved = await postJSON("/push/preferences", { notify_pace_heads_up: on, pace_prompt_answered: true })
                .catch(() => ({ ok: false }));
            if (!saved.ok) {
                card.querySelectorAll("button").forEach((b) => { b.disabled = false; });
                showToast("Couldn't save that. Try again in a moment.", "error");
                return;
            }
            card.remove();
            if (!on) return;
            const here = await turnOnPushHere();
            showToast(here
                ? "Heads-up is on. Artha will tell you if you get ahead of pace."
                : "Heads-up is on. Turn on notifications in Settings to get it in this browser.", "success", 6000);
        });
    });
}

setupPacePrompt();
