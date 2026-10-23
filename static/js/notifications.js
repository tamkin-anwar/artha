// static/js/notifications.js: web push reminders, reminder preferences and
// the calendar link.
//
// Runs on every signed-in page, because an existing push subscription is
// re-sent to the server on each load: a safety net for the browser
// rotating it, alongside the service worker's own pushsubscriptionchange
// handler, that catches drift any time the person is using the site. The
// controls themselves live on the Settings page (templates/settings.html);
// each part below is a no-op where its control isn't on the page.

import { showToast } from "./toast.js";

function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
}

function postJSON(url, body) {
    return fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
        body: JSON.stringify(body),
    });
}

// Web Push wants the VAPID key as a raw Uint8Array, but the server hands
// it over base64url-encoded (the same string browsers themselves use for
// applicationServerKey).
function urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const raw = window.atob(base64);
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

const pushSupported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
const signedIn = Boolean(document.body.dataset.displayCurrency);

// --- Push on this device ----------------------------------------------------

const vapidMeta = document.querySelector('meta[name="vapid-public-key"]');
const vapidPublicKey = vapidMeta ? vapidMeta.content : "";

// Subscribes this browser and saves the subscription on the server, or
// undoes the browser half if the server never saved it, so a retry starts
// clean instead of claiming reminders that will never arrive.
function subscribeHere() {
    return navigator.serviceWorker.ready
        .then((reg) => reg.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: urlBase64ToUint8Array(vapidPublicKey),
        }))
        .then((sub) => postJSON("/push/subscribe", sub.toJSON()).then((res) => {
            if (res.ok) return;
            return sub.unsubscribe().then(() => { throw new Error("server_save_failed"); });
        }));
}

// For a control outside Settings (Today's pace heads-up card): asks this
// browser for notifications if it hasn't been asked, and subscribes.
// Resolves true when this browser will get them, false when it can't or
// the person declined; never rejects.
export function turnOnPushHere() {
    if (!pushSupported || !signedIn || !vapidPublicKey) return Promise.resolve(false);
    const permission = Notification.permission === "granted"
        ? Promise.resolve("granted")
        : Notification.requestPermission();
    return permission
        .then((perm) => (perm === "granted" ? subscribeHere().then(() => true) : false))
        .catch(() => false);
}

function setupPush() {
    if (!pushSupported || !signedIn) return;
    const toggle = document.getElementById("toggle-push");
    const status = document.getElementById("push-status");
    const prefs = document.getElementById("push-preferences");

    function show(subscribed) {
        if (toggle) {
            toggle.dataset.subscribed = subscribed ? "1" : "0";
            toggle.textContent = subscribed ? "Turn off" : "Turn on";
            toggle.hidden = false;
        }
        if (status) {
            status.textContent = subscribed
                ? "On for this browser."
                : Notification.permission === "denied"
                    ? "Blocked in this browser's settings."
                    : "Off for this browser.";
        }
        // A preference is meaningless with no active subscription to
        // apply it to.
        if (prefs) prefs.hidden = !subscribed;
    }

    function subscribe() {
        return subscribeHere().then(() => show(true));
    }

    function unsubscribe() {
        return navigator.serviceWorker.ready
            .then((reg) => reg.pushManager.getSubscription())
            .then((sub) => {
                if (!sub) return;
                const endpoint = sub.endpoint;
                return sub.unsubscribe().then(() => postJSON("/push/unsubscribe", { endpoint }).then((res) => {
                    if (!res.ok) throw new Error("server_remove_failed");
                }));
            })
            .then(() => show(false));
    }

    navigator.serviceWorker.ready
        .then((reg) => reg.pushManager.getSubscription())
        .then((sub) => {
            show(Boolean(sub));
            if (sub) postJSON("/push/subscribe", sub.toJSON());
        })
        .catch(() => {});

    toggle?.addEventListener("click", () => {
        toggle.disabled = true;
        const enabling = toggle.dataset.subscribed !== "1";
        const action = enabling
            ? (Notification.permission === "granted"
                ? subscribe()
                : Notification.requestPermission().then((perm) => {
                    if (perm !== "granted") throw new Error("denied");
                    return subscribe();
                }))
            : unsubscribe();
        action
            .catch((err) => {
                // Declining the browser's prompt is a choice, not an error.
                if (err && err.message === "denied") {
                    show(false);
                    return;
                }
                showToast("Couldn't change reminders. Try again in a moment.", "error");
            })
            .finally(() => { toggle.disabled = false; });
    });
}

// --- Reminder preferences (shared with the iPhone app's settings) -----------

function setupPreferences() {
    function save(payload) {
        return postJSON("/push/preferences", payload).then((res) => {
            if (!res.ok) throw new Error("save_failed");
        });
    }
    document.querySelectorAll("[data-pref]").forEach((box) => {
        box.addEventListener("change", () => {
            const checked = box.checked;
            save({ [box.dataset.pref]: checked }).catch(() => {
                box.checked = !checked;
                showToast("Couldn't save that. Try again in a moment.", "error");
            });
        });
    });
    const hour = document.getElementById("reminder-hour-select");
    if (hour) {
        let previous = hour.value;
        hour.addEventListener("change", () => {
            save({ reminder_hour: parseInt(hour.value, 10) })
                .then(() => { previous = hour.value; })
                .catch(() => {
                    hour.value = previous;
                    showToast("Couldn't save that. Try again in a moment.", "error");
                });
        });
    }
}

// --- Calendar link -----------------------------------------------------------

function setupCalendarLink() {
    const btn = document.getElementById("copy-calendar-link-btn");
    if (!btn) return;
    btn.addEventListener("click", () => {
        const copy = (url) => {
            btn.dataset.calendarFeedUrl = url;
            navigator.clipboard.writeText(url)
                .then(() => showToast("Calendar link copied.", "success"))
                .catch(() => showToast(url, "success"));
        };
        // Issued lazily the first time someone asks for it.
        if (btn.dataset.calendarFeedUrl) {
            copy(btn.dataset.calendarFeedUrl);
            return;
        }
        btn.disabled = true;
        fetch("/calendar/feed/regenerate", { method: "POST", headers: { "X-CSRFToken": csrfToken() } })
            .then((res) => { if (!res.ok) throw new Error("failed"); return res.json(); })
            .then((data) => copy(data.url))
            .catch(() => showToast("Couldn't set up your calendar link. Try again in a moment.", "error"))
            .finally(() => { btn.disabled = false; });
    });
}

setupPush();
setupPreferences();
setupCalendarLink();
