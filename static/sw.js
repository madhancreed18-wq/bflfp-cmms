const CACHE = "bflfp-v451";
const SHELL = ["/", "/manifest.json", "/logo.png",
  "/Logo192.png", "/Logo512.png", "/Logo180.png", "/helpbot.png", "/helpbot-icon.png", "/vendor/jsqr.js", "/vendor/qrcode.js", "/vendor/pdf.min.js", "/vendor/pdf.worker.min.js"];
self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});
self.addEventListener("activate", e => e.waitUntil((async () => {
  const keys = await caches.keys();
  await Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)));  // drop old caches
  await clients.claim();
})()));
self.addEventListener("fetch", e => {
  if (e.request.url.includes("/api/")) return;            // API always live
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));  // network-first, cache fallback offline
});
self.addEventListener("push", e => {
  let d = { title: "BFL Group CMMS", body: "" };
  try { d = e.data.json(); } catch (err) {}
  e.waitUntil(self.registration.showNotification(d.title, {
    body: d.body, icon: "/Logo192.png", badge: "/Logo192.png",
    vibrate: [200, 100, 200], data: d.url || "/"
  }));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = e.notification.data || "/";
  e.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then(ws => {
    for (const c of ws) { if ("focus" in c) { c.focus(); try { c.postMessage({ type: "open-url", url }); } catch (err) {} return; } }
    if (clients.openWindow) return clients.openWindow(url);
  }));
});
