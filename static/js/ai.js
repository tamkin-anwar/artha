// static/js/ai.js
// Artha AI chat widget — handles /api/ai/chat and /api/ai/insights

const widget = document.getElementById("artha-ai-widget");
if (widget) initAI();

function initAI() {
    const messagesEl  = document.getElementById("ai-messages");
    const inputEl     = document.getElementById("ai-input");
    const sendBtn     = document.getElementById("ai-send-btn");
    const insightsBtn = document.getElementById("ai-insights-btn");
    const clearBtn    = document.getElementById("ai-clear-btn");
    const CSRF        = widget.dataset.csrf;

    const EMPTY_STATE_HTML = `
        <div id="ai-empty-state" style="flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:20px; text-align:center;">
            <div class="ai-orb" id="ai-orb"></div>
            <div style="font-family:'Fraunces',serif; font-size:24px; color:var(--text-primary);">Artha AI</div>
            <div style="display:flex; flex-wrap:wrap; gap:8px; justify-content:center; max-width:480px;">
                <button type="button" class="ai-chip" data-chip="insights">Get financial insights</button>
                <button type="button" class="ai-chip" data-chip="spend">How much did I spend this month?</button>
                <button type="button" class="ai-chip" data-chip="savings">What's my savings rate?</button>
            </div>
        </div>`;

    // Client-owned conversation history — sent with every request,
    // never stored on the server.
    let history = [];
    let busy    = false;

    // -----------------------------------------------------------------------
    // Rendering
    // -----------------------------------------------------------------------

    function hideEmpty() {
        document.getElementById("ai-empty-state")?.remove();
    }

    function showClearBtn() {
        clearBtn.style.display = "";
    }

    function scrollToBottom() {
        messagesEl.scrollTo({ top: messagesEl.scrollHeight, behavior: "smooth" });
    }

    function formatText(raw) {
        // Minimal safe rendering: escape HTML, then apply basic markdown.
        // Heading strip runs after escaping (so the <strong> tags it
        // inserts don't themselves get escaped) but before the other
        // markdown replacements.
        return raw
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/^#{1,3}\s+(.+)$/gm, "<strong>$1</strong>")
            .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>")
            .replace(/\*(.*?)\*/g, "<em>$1</em>")
            .replace(/\n/g, "<br>");
    }

    // Walks the already-escaped/markdown'd DOM fragment and wraps each
    // word of text in its own span with a staggered animation-delay, so
    // the response reads as if it's arriving word by word — without ever
    // touching raw HTML strings (avoids breaking <strong>/<em>/<br> tags).
    function wrapWordsForAnimation(root) {
        let wordIndex = 0;
        const MAX_STAGGERED = 120; // cap so very long replies don't trail off for seconds

        function walk(node) {
            if (node.nodeType === Node.TEXT_NODE) {
                const parts = node.textContent.split(/(\s+)/);
                const frag = document.createDocumentFragment();
                parts.forEach((chunk) => {
                    if (chunk === "") return;
                    if (/^\s+$/.test(chunk)) {
                        frag.appendChild(document.createTextNode(chunk));
                        return;
                    }
                    const span = document.createElement("span");
                    span.className = "ai-word";
                    span.style.animationDelay = (Math.min(wordIndex, MAX_STAGGERED) * 28) + "ms";
                    span.textContent = chunk;
                    frag.appendChild(span);
                    wordIndex++;
                });
                node.replaceWith(frag);
            } else if (node.nodeType === Node.ELEMENT_NODE) {
                Array.from(node.childNodes).forEach(walk);
            }
        }

        Array.from(root.childNodes).forEach(walk);
    }

    function appendMessage(role, text, onRendered) {
        if (role === "user") {
            hideEmpty();
            showClearBtn();

            const row = document.createElement("div");
            row.className = "ai-row user";
            const bubble = document.createElement("div");
            bubble.className = "ai-bubble-user";
            bubble.textContent = text;
            row.appendChild(bubble);
            messagesEl.appendChild(row);
            scrollToBottom();
            return;
        }

        // Assistant: if a loading orb is present, let it play a brief
        // "speaking" pulse (thinking -> speaking) before swapping in the
        // real message, matching the idle -> thinking -> speaking ->
        // rendered sequence.
        const loadingOrb = document.getElementById("ai-loading-orb");

        const renderNow = () => {
            removeLoading();

            // A reply can be empty when the model only proposed a tool
            // action with no accompanying text — skip the bubble so the
            // action card isn't preceded by a blank one.
            if (text && text.trim()) {
                const row = document.createElement("div");
                row.className = "ai-row assistant";

                const avatar = document.createElement("div");
                avatar.className = "ai-avatar-dot";
                avatar.setAttribute("aria-hidden", "true");

                const bubble = document.createElement("div");
                bubble.className = "ai-bubble-assistant";
                bubble.innerHTML = formatText(text);
                wrapWordsForAnimation(bubble);

                row.appendChild(avatar);
                row.appendChild(bubble);
                messagesEl.appendChild(row);
            }
            scrollToBottom();
            if (typeof onRendered === "function") onRendered();
        };

        if (loadingOrb) {
            loadingOrb.classList.remove("thinking");
            loadingOrb.classList.add("speaking");
            setTimeout(renderNow, 450);
        } else {
            renderNow();
        }
    }

    function appendLoading() {
        hideEmpty();
        const row = document.createElement("div");
        row.id = "ai-loading-row";
        row.className = "ai-row assistant";

        const orb = document.createElement("div");
        orb.className = "ai-orb ai-orb-mini thinking";
        orb.id = "ai-loading-orb";

        row.appendChild(orb);
        messagesEl.appendChild(row);
        scrollToBottom();
    }

    function removeLoading() {
        document.getElementById("ai-loading-row")?.remove();
    }

    // Describes an "add_transaction" proposal: what the card shows, and
    // what Confirm actually submits. Goes straight to the existing
    // /add_transaction route (same CSRF, same server-side validation as
    // the manual form) — the AI never has a path to the database itself.
    function describeTransactionAction(p) {
        const description = String(p.description || "Transaction");
        const amountNum    = Number.parseFloat(p.amount);
        const type         = p.type === "income" ? "income" : "expense";
        const category     = typeof p.category === "string" ? p.category : "";
        const date         = typeof p.date === "string" && p.date ? p.date : null;

        const metaParts = [type === "income" ? "Income" : "Expense"];
        if (category) metaParts.push(category.charAt(0).toUpperCase() + category.slice(1));
        metaParts.push(date || "Today");

        return {
            title: description,
            tag: {
                text: (type === "income" ? "+" : "−") + "$" + (Number.isFinite(amountNum) ? amountNum.toFixed(2) : "0.00"),
                color: type === "income" ? "var(--emerald)" : "var(--red)",
            },
            meta: metaParts.join(" · "),
            successText: "Added to your transactions",
            execute: async () => {
                const formData = new FormData();
                formData.append("description", description);
                formData.append("amount", Number.isFinite(amountNum) ? String(amountNum) : "0");
                formData.append("type", type);
                if (category) formData.append("category", category);
                if (date) formData.append("date", date);
                formData.append("csrf_token", CSRF);

                const res = await fetch("/add_transaction", {
                    method: "POST",
                    credentials: "same-origin",
                    headers: { "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": CSRF },
                    body: formData,
                });
                if (res.ok) return true;
                const data = await res.json().catch(() => null);
                return data?.message || "Could not add the transaction.";
            },
        };
    }

    // Describes a "create_note" proposal. Confirm chains the same two
    // calls the Notes page itself makes for a brand-new note: create a
    // blank note, then save its fields.
    function describeNoteAction(p) {
        const title   = String(p.title || "Note");
        const content = String(p.content || "");
        const color   = typeof p.color === "string" ? p.color : "";
        const dueDate = typeof p.due_date === "string" && p.due_date ? p.due_date : "";

        const preview = content.length > 80 ? content.slice(0, 77) + "..." : content;
        const metaParts = [preview || "No content"];
        if (dueDate) metaParts.push("Due " + dueDate);

        return {
            title,
            meta: metaParts.join(" · "),
            successText: "Added to your notes",
            execute: async () => {
                const createRes = await fetch("/notes/new", {
                    method: "POST",
                    credentials: "same-origin",
                    headers: { "X-CSRFToken": CSRF },
                });
                if (!createRes.ok) return "Could not create the note.";
                const created = await createRes.json().catch(() => null);
                if (!created?.id) return "Could not create the note.";

                const payload = { title, content };
                if (color) payload.color = color;
                if (dueDate) payload.due_date = dueDate;

                const updateRes = await fetch("/notes/" + created.id + "/update", {
                    method: "PATCH",
                    credentials: "same-origin",
                    headers: { "Content-Type": "application/json", "X-CSRFToken": CSRF },
                    body: JSON.stringify(payload),
                });
                if (updateRes.ok) return true;
                const data = await updateRes.json().catch(() => null);
                return data?.message || "Could not save the note.";
            },
        };
    }

    // Describes a "create_event" proposal. Confirm submits straight to
    // the existing /calendar/events route, same as the New Event flow.
    function describeEventAction(p) {
        const title      = String(p.title || "Event");
        const start      = typeof p.start === "string" ? p.start : "";
        const end        = typeof p.end === "string" ? p.end : "";
        const color      = typeof p.color === "string" ? p.color : "";
        const recurrence = typeof p.recurrence === "string" ? p.recurrence : "";

        const metaParts = [formatEventRange(start, end)];
        if (recurrence) metaParts.push(recurrence.charAt(0).toUpperCase() + recurrence.slice(1));

        return {
            title,
            meta: metaParts.join(" · "),
            successText: "Added to your calendar",
            execute: async () => {
                const res = await fetch("/calendar/events", {
                    method: "POST",
                    credentials: "same-origin",
                    headers: { "Content-Type": "application/json", "X-CSRFToken": CSRF },
                    body: JSON.stringify({ title, start, end, color: color || undefined, recurrence: recurrence || undefined }),
                });
                if (res.ok) return true;
                const data = await res.json().catch(() => null);
                return data?.message || "Could not add the event.";
            },
        };
    }

    // Describes a "set_budget" proposal. Confirm submits straight to the
    // existing /finance/budget or /finance/category-budget route (same
    // validation as the manual Finance-page forms), picking whichever one
    // based on whether a category was proposed.
    function describeBudgetAction(p) {
        const amountNum = Number.parseFloat(p.amount);
        const category  = typeof p.category === "string" ? p.category : "";
        const amountStr = Number.isFinite(amountNum) ? amountNum.toFixed(2) : "0.00";

        return {
            title: category ? (category.charAt(0).toUpperCase() + category.slice(1) + " budget") : "Monthly budget",
            meta: "$" + amountStr + " / month",
            successText: "Budget saved",
            execute: async () => {
                const formData = new FormData();
                formData.append("monthly_cap", Number.isFinite(amountNum) ? String(amountNum) : "0");
                if (category) formData.append("category", category);
                formData.append("csrf_token", CSRF);

                const res = await fetch(category ? "/finance/category-budget" : "/finance/budget", {
                    method: "POST",
                    credentials: "same-origin",
                    headers: { "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": CSRF },
                    body: formData,
                });
                if (res.ok) return true;
                const data = await res.json().catch(() => null);
                return data?.message || "Could not save the budget.";
            },
        };
    }

    function formatEventRange(start, end) {
        const s = new Date(start);
        if (isNaN(s.getTime())) return start || "Unknown time";
        const e = new Date(end);
        const dateStr   = s.toLocaleDateString(undefined, { month: "short", day: "numeric" });
        const startTime = s.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
        const endTime   = !isNaN(e.getTime()) ? e.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }) : "";
        return dateStr + ", " + startTime + (endTime ? " - " + endTime : "");
    }

    // Shared confirmation-card shell: renders whatever describeXAction()
    // returned, and owns the Confirm/Cancel interaction. describe.execute()
    // resolves to true (success), an error message string, or throws on a
    // genuine network failure — the shell turns any of those into the
    // right on-card feedback.
    function buildActionCard(describe) {
        const row = document.createElement("div");
        row.className = "ai-row assistant";

        const avatar = document.createElement("div");
        avatar.className = "ai-avatar-dot";
        avatar.setAttribute("aria-hidden", "true");

        const card = document.createElement("div");
        card.style.cssText =
            "background:var(--bg-card); border:1px solid var(--border-subtle); " +
            "border-radius:12px; padding:14px 16px; max-width:360px; display:flex; " +
            "flex-direction:column; gap:10px;";

        const topRow = document.createElement("div");
        topRow.style.cssText = "display:flex; justify-content:space-between; align-items:baseline; gap:12px;";

        const label = document.createElement("span");
        label.textContent = describe.title;
        label.style.cssText = "color:var(--text-primary); font-weight:600;";
        topRow.appendChild(label);

        if (describe.tag) {
            const tag = document.createElement("span");
            tag.textContent = describe.tag.text;
            tag.style.cssText =
                "font-variant-numeric:tabular-nums; font-weight:600; white-space:nowrap; color:" + describe.tag.color + ";";
            topRow.appendChild(tag);
        }

        const meta = document.createElement("div");
        meta.style.cssText = "color:var(--text-secondary); font-size:13px;";
        meta.textContent = describe.meta;

        const buttons = document.createElement("div");
        buttons.style.cssText = "display:flex; gap:8px;";

        const confirmBtn = document.createElement("button");
        confirmBtn.type = "button";
        confirmBtn.className = "btn-primary";
        confirmBtn.textContent = "Confirm";

        const cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "btn-secondary";
        cancelBtn.textContent = "Cancel";

        buttons.appendChild(confirmBtn);
        buttons.appendChild(cancelBtn);

        card.appendChild(topRow);
        card.appendChild(meta);
        card.appendChild(buttons);

        cancelBtn.addEventListener("click", () => {
            card.style.opacity = "0.5";
            buttons.remove();
        });

        confirmBtn.addEventListener("click", async () => {
            confirmBtn.disabled = true;
            cancelBtn.disabled  = true;

            try {
                const result = await describe.execute();
                buttons.remove();
                meta.remove();

                const feedback = document.createElement("div");
                if (result === true) {
                    feedback.style.cssText = "color:var(--emerald); font-size:13px; font-weight:600;";
                    feedback.textContent = describe.successText;
                } else {
                    feedback.style.cssText = "color:var(--red); font-size:13px;";
                    feedback.textContent = (typeof result === "string" && result) || "Could not complete this action.";
                }
                card.appendChild(feedback);
            } catch {
                buttons.remove();
                const errEl = document.createElement("div");
                errEl.style.cssText = "color:var(--red); font-size:13px;";
                errEl.textContent = "Network error. Please try again.";
                card.appendChild(errEl);
            }
        });

        row.appendChild(avatar);
        row.appendChild(card);
        messagesEl.appendChild(row);
        scrollToBottom();
    }

    // Renders whichever action type the assistant proposed. Every type is
    // a proposal only: nothing here writes to the database, that only
    // happens from inside describe.execute() when the user clicks Confirm.
    function renderActionCard(action) {
        if (!action || !action.type) return;
        const p = action.params || {};

        let describe;
        if (action.type === "add_transaction") describe = describeTransactionAction(p);
        else if (action.type === "create_note") describe = describeNoteAction(p);
        else if (action.type === "create_event") describe = describeEventAction(p);
        else if (action.type === "set_budget") describe = describeBudgetAction(p);
        else return;

        buildActionCard(describe);
    }

    function appendError(msg) {
        removeLoading();

        const row = document.createElement("div");
        row.className = "ai-row assistant";

        const avatar = document.createElement("div");
        avatar.className = "ai-avatar-dot";
        avatar.style.background = "var(--red)";
        avatar.setAttribute("aria-hidden", "true");

        const bubble = document.createElement("div");
        bubble.className = "ai-bubble-assistant";
        bubble.style.color = "var(--red)";
        bubble.textContent = msg;

        row.appendChild(avatar);
        row.appendChild(bubble);
        messagesEl.appendChild(row);
        scrollToBottom();
    }

    // -----------------------------------------------------------------------
    // State management
    // -----------------------------------------------------------------------

    function setBusy(state) {
        busy              = state;
        sendBtn.disabled     = state;
        insightsBtn.disabled = state;
        inputEl.disabled     = state;
        if (state) {
            appendLoading();
        }
        // Note: going busy->false no longer removes the loading row here —
        // appendMessage() owns that transition (thinking -> speaking ->
        // rendered). appendError() removes it immediately on failure.
    }

    // -----------------------------------------------------------------------
    // API: chat
    // -----------------------------------------------------------------------

    async function sendMessage(message) {
        if (busy || !message.trim()) return;

        appendMessage("user", message);
        const historySnapshot = [...history];
        history.push({ role: "user", content: message });
        setBusy(true);

        try {
            const res  = await fetch("/api/ai/chat", {
                method:  "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken":  CSRF,
                },
                body: JSON.stringify({ message, history: historySnapshot }),
            });

            const data = await res.json();
            setBusy(false);

            if (!res.ok || data.error) {
                appendError(data.error || "Something went wrong. Please try again.");
                history.pop();
                return;
            }

            appendMessage("assistant", data.reply, () => {
                (data.pending_actions || []).forEach(renderActionCard);
            });
            history.push({ role: "assistant", content: data.reply });

        } catch {
            setBusy(false);
            appendError("Network error. Check your connection.");
            history.pop();
        }
    }

    // -----------------------------------------------------------------------
    // API: insights
    // -----------------------------------------------------------------------

    async function fetchInsights() {
        if (busy) return;
        hideEmpty();
        setBusy(true);

        try {
            const res  = await fetch("/api/ai/insights", {
                method:  "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken":  CSRF,
                },
            });

            const data = await res.json();
            setBusy(false);

            if (!res.ok || data.error) {
                appendError(data.error || "Could not generate insights.");
                return;
            }

            appendMessage("assistant", data.insights);
            history.push({ role: "assistant", content: data.insights });

        } catch {
            setBusy(false);
            appendError("Network error. Check your connection.");
        }
    }

    // -----------------------------------------------------------------------
    // Clear conversation
    // -----------------------------------------------------------------------

    function clearConversation() {
        if (busy) return;
        history = [];
        messagesEl.innerHTML = EMPTY_STATE_HTML;
        clearBtn.style.display = "none";
    }

    // -----------------------------------------------------------------------
    // Input auto-resize (up to 5 lines, capped via CSS max-height)
    // -----------------------------------------------------------------------

    function autoResizeInput() {
        inputEl.style.height = "auto";
        inputEl.style.height = inputEl.scrollHeight + "px";
    }
    inputEl.addEventListener("input", autoResizeInput);

    // -----------------------------------------------------------------------
    // Event listeners
    // -----------------------------------------------------------------------

    sendBtn.addEventListener("click", () => {
        const msg = inputEl.value.trim();
        if (!msg) return;
        inputEl.value = "";
        autoResizeInput();
        sendMessage(msg);
    });

    inputEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            const msg = inputEl.value.trim();
            if (!msg) return;
            inputEl.value = "";
            autoResizeInput();
            sendMessage(msg);
        }
    });

    insightsBtn.addEventListener("click", fetchInsights);
    clearBtn.addEventListener("click", clearConversation);

    // Event delegation for suggestion chips — survives clearConversation()
    // rebuilding the empty-state markup, since messagesEl itself never
    // gets replaced, only its children.
    messagesEl.addEventListener("click", (e) => {
        const chip = e.target.closest(".ai-chip");
        if (!chip || busy) return;

        const kind = chip.dataset.chip;
        if (kind === "insights") {
            fetchInsights();
        } else if (kind === "spend") {
            sendMessage("How much did I spend this month?");
        } else if (kind === "savings") {
            sendMessage("What's my savings rate?");
        }
    });
}
