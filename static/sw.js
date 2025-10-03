// sw.js — Mochitos (push + local test + fast activation)
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'LOCAL_TEST') {
    self.registration.showNotification('Local OK 🔔', {
      body: 'Mostrada desde el Service Worker',
      icon: '/static/icons/icon-192.png',
      badge: '/static/icons/badge-72.png'
    });
  }
});
self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch(e){}
  const title = data.title || 'Notificación';
  const body  = data.body  || 'Tienes una novedad';
  const icon  = data.icon  || '/static/icons/icon-192.png';
  const badge = data.badge || '/static/icons/badge-72.png';
  const url   = data.url   || '/';
  event.waitUntil(self.registration.showNotification(title, { body, icon, badge, data: { url } }));
});
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification && event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
      for (const c of list) { if (c.url.includes(url) && 'focus' in c) return c.focus(); }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});