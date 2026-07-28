// Minimal service worker — makes the dashboard installable as an app
// (Android/Chrome requires a fetch handler to offer "Install app"). The
// data itself (scans, results, settings) is always live from the Flask
// server; this only lets the app *shell* open instantly and survive a
// brief network drop. Network-first for the page, cache-first for icons.
const CACHE = "job-scanner-shell-v1";
const SHELL_URLS = ["/", "/static/manifest.json", "/static/icons/icon-192.png", "/static/icons/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL_URLS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  // Never cache the live API — scan status/results/log must always be fresh.
  if (url.pathname.startsWith("/api/")) return;

  if (url.pathname.startsWith("/static/icons/")) {
    event.respondWith(caches.match(event.request).then((hit) => hit || fetch(event.request)));
    return;
  }

  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE).then((c) => c.put(event.request, copy));
        return resp;
      })
      .catch(() => caches.match(event.request))
  );
});
