self.addEventListener('install', (e) => {
  self.skipWaiting();
});
self.addEventListener('activate', (e) => {
  e.waitUntil(self.clients.claim());
});

self.addEventListener('push', (e) => {
  let data = {};
  try { data = e.data ? e.data.json() : {}; } catch {}
  const title = data.title || '💌 Nuevo aviso';
  const options = {
    body: data.body || 'Tienes una notificación',
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/badge-72.png',
    data: data.data && data.data.url ? data.data.url : '/'
  };
  e.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || e.notification.data || '/';
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      const client = list.find(c => c.url === url);
      return client ? client.focus() : clients.openWindow(url);
    })
  );
});
