// static/sw.js
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch(e){}
  const title = data.title || "Notificación";
  const body  = data.body  || "";
  const extra = data.data  || {};

  const options = {
    body,
    icon: "/favicon.ico",
    badge: "/favicon.ico",
    data: extra,
    vibrate: [100, 50, 100],
    actions: [
      { action: "open", title: "Abrir" },
      { action: "dismiss", title: "Cerrar" }
    ]
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  if (event.action === "dismiss") return;
  const url = "/";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      for (const w of wins) { if ("focus" in w) { w.focus(); return; } }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
