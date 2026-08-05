const CACHE = "bflfp-v1";
self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(["/", "/manifest.json"])));
  self.skipWaiting();
});
self.addEventListener("activate", e => e.waitUntil(clients.claim()));
self.addEventListener("fetch", e => {
  if (e.request.url.includes("/api/")) return;            // API always live
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
self.addEventListener("push", e => {
  let d = { title: "BFLFP", body: "" };
  try { d = e.data.json(); } catch (err) {}
  e.waitUntil(self.registration.showNotification(d.title, {
    body: d.body, icon: "/icon-192.png", badge: "/icon-192.png",
    vibrate: [200, 100, 200], data: d.url || "/"
  }));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil(clients.matchAll({ type: "window" }).then(ws =>
    ws.length ? ws[0].focus() : clients.openWindow(e.notification.data || "/")));
});
