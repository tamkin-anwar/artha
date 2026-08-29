// static/service-worker.js

const CACHE_NAME = "artha-cache-v27";
const OFFLINE_URL = "/static/offline.html";

const ASSETS_TO_CACHE = [
  // Offline fallback page (must be a static asset)
    OFFLINE_URL,

    // PWA
    "/static/manifest.json",

    // Icons (match your manifest)
    "/static/icons/favicon.ico",
    "/static/icons/favicon-16.png",
    "/static/icons/favicon-32.png",
    "/static/icons/apple-touch-icon-180.png",
    "/static/icons/icon-192.png",
    "/static/icons/icon-512.png",

    // CSS
    "/static/css/style.css",
    "/static/css/toast.css",

    // Vendor (LOCAL)
    "/static/vendor/chart.umd.min.js",
    "/static/vendor/chartjs-plugin-datalabels.min.js",
    "/static/vendor/sortable.min.js",
    "/static/vendor/math.min.js",

    // JS modules
    "/static/js/init.js",
    "/static/js/flash.js",
    "/static/js/notes.js",
    "/static/js/transactions.js",
    "/static/js/theme.js",
    "/static/js/settings.js",
    "/static/js/ai.js",
    "/static/js/chart.js",
    "/static/js/toast.js",
    "/static/js/search.js",
    "/static/js/csv_import.js",
    ];

    self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS_TO_CACHE))
    );
    self.skipWaiting();
    });

    self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys().then((keys) =>
        Promise.all(
            keys.map((key) => (key !== CACHE_NAME ? caches.delete(key) : null))
        )
        )
    );
    self.clients.claim();
    });

    self.addEventListener("fetch", (event) => {
    // Don’t touch non-GET requests (POST/PUT/etc). Let the network handle them.
    if (event.request.method !== "GET") return;

    // Navigations: network first, fallback to offline page
    if (event.request.mode === "navigate") {
        event.respondWith(
        fetch(event.request).catch(() => caches.match(OFFLINE_URL))
        );
        return;
    }

    // Static assets: network first, falling back to cache only when the
    // network fetch itself fails (genuinely offline). The previous
    // cache-first strategy shipped one real bug: the browser only
    // re-runs this file's install step when service-worker.js itself
    // changes byte-for-byte, so a CSS/JS-only deploy (the overwhelming
    // majority of them) never re-triggered it, and an already-visiting
    // user's Service Worker kept serving the exact response it cached
    // the first time it ever ran, sometimes for weeks, surviving even a
    // hard reload, since the Service Worker intercepts the request
    // before the browser's own cache-busting ever gets a say. Confirmed
    // live in production (2026-08-28): a sidebar CSS change rendered
    // correctly once, then reverted to the old broken layout on a later
    // reload, on the same deploy, with no code change in between.
    // Network-first still updates the cache on every successful fetch
    // (so the offline fallback below stays reasonably fresh), it just
    // no longer lets a stale response win over a reachable network.
    event.respondWith(
        fetch(event.request)
            .then((response) => {
                if (response.ok) {
                    const responseCopy = response.clone();
                    caches.open(CACHE_NAME).then((cache) => cache.put(event.request, responseCopy));
                }
                return response;
            })
            .catch(() => caches.match(event.request))
    );
});

// Renewal reminders — the server sends { title, body, url } as the push
// payload (see artha/services/push_service.py). event.data can be missing
// entirely (some push services deliver empty "wake up and check" pings),
// so this always falls back to a generic notification rather than
// silently doing nothing.
self.addEventListener("push", (event) => {
    let payload = {};
    try {
        payload = event.data ? event.data.json() : {};
    } catch (e) {
        payload = {};
    }

    const title = payload.title || "Artha";
    const options = {
        body: payload.body || "You have a reminder.",
        icon: "/static/icons/icon-192.png",
        badge: "/static/icons/icon-192.png",
        data: { url: payload.url || "/" },
    };

    event.waitUntil(self.registration.showNotification(title, options));
});

// Chrome/FCM can silently invalidate and rotate a subscription's
// endpoint on its own — no user action, no page even open — which is
// exactly the flakiness that made reminders stop arriving after working
// once. This is the browser's own signal that it happened; without a
// handler here the server keeps a dead endpoint forever, since nothing
// else would ever tell it the subscription changed.
self.addEventListener("pushsubscriptionchange", (event) => {
    const resubscribe = self.registration.pushManager
        .subscribe(event.oldSubscription ? event.oldSubscription.options : { userVisibleOnly: true })
        .then((newSubscription) =>
            fetch("/push/subscribe", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(newSubscription.toJSON()),
            })
        );
    event.waitUntil(resubscribe);
});

// Focuses an already-open Artha tab if one exists rather than always
// opening a new one — most people already have it open in a pinned tab.
self.addEventListener("notificationclick", (event) => {
    event.notification.close();
    const targetUrl = (event.notification.data && event.notification.data.url) || "/";

    event.waitUntil(
        self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientList) => {
            for (const client of clientList) {
                if ("focus" in client) return client.focus();
            }
            if (self.clients.openWindow) return self.clients.openWindow(targetUrl);
        })
    );
});