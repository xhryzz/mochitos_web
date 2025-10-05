// sw.js — Mochitos (es-ES) — push + local + activación rápida
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

// Prueba local enviada desde la página
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'LOCAL_TEST') {
    self.registration.showNotification('Notificación local 🔔', {
      body: 'Mostrada desde el Service Worker',
      icon: '/static/icons/icon-192.png',
      badge: '/static/icons/badge-72.png',
      lang: 'es-ES'
    });
  }
});

// Push real desde el servidor
self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch(e){}

  const titulo = data.title || 'Notificación';
  // No usar fallback genérico: si el server no manda body, lo omitimos
  const cuerpo = ('body' in data) ? data.body : undefined;
  const icono  = data.icon  || '/static/icons/icon-192.png';
  const badge  = data.badge || '/static/icons/badge-72.png';
  const url    = data.url   || '/';
  const tag    = data.tag   || undefined;

  event.waitUntil(
    self.registration.showNotification(titulo, {
      body: cuerpo,
      icon: icono,
      badge: badge,
      data: { url },
      tag,                 // agrupa notificaciones del mismo tipo
      lang: 'es-ES'
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification && event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
      for (const c of list) {
        if (c.url.includes(url) && 'focus' in c) return c.focus();
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
