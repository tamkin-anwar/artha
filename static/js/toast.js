// static/js/toast.js
// Accessible, stackable toast notifications with optional action button (e.g., Undo)

let toastQueue = [];
const MAX_TOASTS = 5;

// Known raw messages -> premium {title, message} pairs. Anything not in
// this table falls back to a generic title (derived from `type`) with the
// raw message as the body, so every call site keeps working untouched —
// showToast()'s signature and every existing caller are unchanged.
const MESSAGE_MAP = {
    // Transactions
    "Transaction added!": { title: "Saved", message: "Transaction recorded successfully" },
    "Transaction updated successfully": { title: "Saved", message: "Transaction recorded successfully" },
    "Transaction deleted": { title: "Deleted", message: "Transaction removed" },
    "Transaction restored.": { title: "Restored", message: "Transaction added back" },
    "Error adding transaction": { title: "Error", message: "Could not save. Please try again" },
    "Transaction update failed": { title: "Error", message: "Could not save. Please try again" },
    "Database error": { title: "Error", message: "Could not save. Please try again" },
    "Network error while updating transaction": { title: "Error", message: "Check your connection and try again" },
    "Network error while deleting transaction": { title: "Error", message: "Check your connection and try again" },
    "Network error while undoing delete": { title: "Error", message: "Check your connection and try again" },
    "Please provide valid details": { title: "Missing details", message: "Fill in a description and amount first" },
    "Order saved": { title: "Saved", message: "New transaction order remembered" },
    "Could not save order": { title: "Error", message: "Could not save the new order" },
    "Failed to save order": { title: "Error", message: "Could not save the new order" },
    "Delete failed": { title: "Error", message: "Could not delete. Please try again" },
    "Undo failed": { title: "Error", message: "Could not undo that action" },

    // Notes
    "Note updated": { title: "Saved", message: "Note recorded" },
    "Note deleted": { title: "Deleted", message: "Note removed" },
    "Note restored.": { title: "Restored", message: "Note added back" },
    "Note cannot be empty": { title: "Missing text", message: "Write something before saving" },
    "Note update failed": { title: "Error", message: "Could not save. Please try again" },
    "Network error while saving note": { title: "Error", message: "Check your connection and try again" },
    "Network error while undoing": { title: "Error", message: "Check your connection and try again" },
    "Network error while saving order": { title: "Error", message: "Could not save the new order" },
    "Reorder failed": { title: "Error", message: "Could not save the new order" },

    // Scenarios / auth currently arrive via server-side flash redirects,
    // not showToast() — these entries are here so they render premium the
    // moment (if ever) something calls showToast() with this exact text.
    "Scenario created!": { title: "Scenario created", message: "What-if added to your simulator" },
    "Scenario updated!": { title: "Saved", message: "Scenario updated" },
    "Password changed successfully.": { title: "Done", message: "Password updated successfully" },
};

const TYPE_ICONS = {
    success: "✓",
    error: "✕",
    info: "✦",
    warning: "⚠",
};

const TYPE_FALLBACK_TITLES = {
    success: "Success",
    error: "Error",
    info: "Notice",
    warning: "Warning",
};

function resolveContent(message, type) {
    const mapped = MESSAGE_MAP[message];
    if (mapped) return mapped;
    return { title: TYPE_FALLBACK_TITLES[type] || "Notice", message: String(message) };
}

/**
 * Show a toast notification
 * @param {string} message
 * @param {"success"|"error"|"info"|"warning"} type
 * @param {number} duration - ms (set 0 to persist until manual close)
 * @param {{ actionText?: string, onAction?: Function }} options
 */
export function showToast(message, type = "info", duration = 3000, options = {}) {
    let container = document.getElementById("toast-container");
    if (!container) {
        container = document.createElement("div");
        container.id = "toast-container";
        container.className = "toast-container";
        container.setAttribute("aria-live", "polite");
        document.body.appendChild(container);
    }

    const validType = TYPE_ICONS[type] ? type : "info";
    const content = resolveContent(message, validType);

    // If a toast with this exact (resolved) message is already visible,
    // don't stack a second one — just reset its auto-dismiss timer. This
    // is what keeps rapid-fire identical toasts (e.g. saving several
    // transactions back to back) from piling up.
    const existing = toastQueue.find(
        (t) => t.dataset.toastMessage === content.message && !t.classList.contains("exit")
    );
    if (existing && typeof existing.__resetTimer === "function") {
        existing.__resetTimer(duration);
        return existing;
    }

    // Limit queue size
    if (toastQueue.length >= MAX_TOASTS) {
        const oldestToast = toastQueue.shift();
        if (oldestToast) removeToast(oldestToast);
    }

    const toast = document.createElement("div");
    toast.dataset.toastMessage = content.message;
    toast.className = `toast toast-${validType}`;
    toast.setAttribute("role", "alert");
    toast.tabIndex = 0;

    // Icon
    const icon = document.createElement("span");
    icon.className = "toast-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = TYPE_ICONS[validType];
    toast.appendChild(icon);

    // Title + message
    const body = document.createElement("div");
    body.className = "toast-body";

    const titleEl = document.createElement("p");
    titleEl.className = "toast-title";
    titleEl.textContent = content.title;
    body.appendChild(titleEl);

    const msgEl = document.createElement("p");
    msgEl.className = "toast-message";
    msgEl.textContent = content.message;
    body.appendChild(msgEl);

    toast.appendChild(body);

    // Optional action button (Undo, Retry, etc.)
    if (options?.actionText && typeof options?.onAction === "function") {
        const actionBtn = document.createElement("button");
        actionBtn.type = "button";
        actionBtn.className = "toast-action-btn";
        actionBtn.textContent = options.actionText;
        actionBtn.addEventListener("click", () => {
            try {
                options.onAction();
            } finally {
                removeToast(toast);
            }
        });

        // Keyboard accessibility
        actionBtn.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                try {
                    options.onAction();
                } finally {
                    removeToast(toast);
                }
            }
        });

        toast.appendChild(actionBtn);
    }

    // Close button
    const closeBtn = document.createElement("button");
    closeBtn.className = "close-btn";
    closeBtn.setAttribute("aria-label", "Dismiss message");
    closeBtn.innerHTML = "&times;";
    closeBtn.type = "button";
    closeBtn.addEventListener("click", () => removeToast(toast));
    closeBtn.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            removeToast(toast);
        }
    });
    toast.appendChild(closeBtn);

    // Auto-dismiss progress bar (only when it will actually auto-dismiss)
    if (duration > 0) {
        const progress = document.createElement("div");
        progress.className = "toast-progress";
        progress.style.animationDuration = `${duration}ms`;
        toast.appendChild(progress);
    }

    toastQueue.push(toast);
    container.appendChild(toast);

    // Trigger animation
    void toast.offsetWidth;
    toast.classList.add("visible");

    // Auto-remove (if duration > 0), pause on hover
    let autoRemoveTimeout = null;
    let currentDuration = duration;

    const startTimer = (d = currentDuration) => {
        currentDuration = d;
        if (d <= 0) return;
        autoRemoveTimeout = setTimeout(() => removeToast(toast), d);
    };

    const stopTimer = () => {
        if (autoRemoveTimeout) clearTimeout(autoRemoveTimeout);
        autoRemoveTimeout = null;
    };

    // Called instead of creating a duplicate toast when one with the same
    // message is already on screen — restarts both the dismiss timer and
    // the draining progress bar so it reads as "still fresh", not stale.
    toast.__resetTimer = (newDuration = currentDuration) => {
        stopTimer();
        const progress = toast.querySelector(".toast-progress");
        if (progress && newDuration > 0) {
            progress.style.animation = "none";
            void progress.offsetWidth;
            progress.style.animationDuration = `${newDuration}ms`;
            progress.style.animation = "";
        }
        startTimer(newDuration);
    };

    startTimer();

    toast.addEventListener("mouseenter", () => {
        stopTimer();
        const progress = toast.querySelector(".toast-progress");
        if (progress) progress.style.animationPlayState = "paused";
    });
    toast.addEventListener("mouseleave", () => {
        startTimer();
        const progress = toast.querySelector(".toast-progress");
        if (progress) progress.style.animationPlayState = "running";
    });

    toast.addEventListener("transitionend", (e) => {
        if (e.propertyName === "opacity" && toast.classList.contains("exit")) {
            toast.remove();
            toastQueue = toastQueue.filter(t => t !== toast);
        }
    });

    return toast;
}

function removeToast(toast) {
    if (!toast) return;
    toast.classList.remove("visible");
    toast.classList.add("exit");
}
