// /sw.js — precache + estrategias rápidas
const PRECACHE_VERSION = 'v3';
const PRECACHE_NAME = 'mochitos-precache-' + PRECACHE_VERSION;
const RUNTIME_NAME = 'mochitos-runtime';

const CORE_ASSETS = [
  '/',                // landing
  '/manifest.webmanifest',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/apple-touch-icon-180.png',
  '/static/icons/badge-72.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(PRECACHE_NAME).then(cache => cache.addAll(CORE_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    // Navigation preload acelera primeros fetch
    if (self.registration.navigationPreload) {
      try { await self.registration.navigationPreload.enable(); } catch {}
    }
    const keys = await caches.keys();
    await Promise.all(keys.map(k => (k.startsWith('mochitos-precache-') && k !== PRECACHE_NAME) ? caches.delete(k) : null));
    await self.clients.claim();
  })());
});

// Stale-While-Revalidate para estáticos, Network-First para HTML
self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Solo mismo origen
  if (url.origin !== location.origin) return;

  // Notificaciones push (ya estaba)
  if (req.headers.get('accept')?.includes('text/event-stream')) return;

  if (req.destination === 'document' || req.mode === 'navigate') {
    event.respondWith((async () => {
      // Intenta usar navigationPreload si existe
      const preload = await event.preloadResponse;
      if (preload) return preload;
      try {
        const net = await fetch(req);
        const cache = await caches.open(RUNTIME_NAME);
        cache.put(req, net.clone());
        return net;
      } catch {
        const cache = await caches.open(RUNTIME_NAME);
        const cached = await cache.match(req) || await caches.match('/');
        return cached || Response.error();
      }
    })());
    return;
  }

  // Estáticos: stale-while-revalidate
  if (url.pathname.startsWith('/static/')) {
    event.respondWith((async () => {
      const cache = await caches.open(RUNTIME_NAME);
      const cached = await cache.match(req);
      const fetchPromise = fetch(req).then((response) => {
        cache.put(req, response.clone());
        return response;
      }).catch(() => null);
      return cached || fetchPromise || Response.error();
    })());
  }
});

self.addEventListener('push', (e) => {
  let data = {};
  try { data = e.data ? e.data.json() : {}; } catch {}
  const title = data.title || '💌 Nuevo aviso';
  const options = {
    body: data.body || 'Tienes una notificación',
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/badge-72.png',
    data: data.url || '/'
  };
  e.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = e.notification?.data || '/';
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clients => {
    for (const c of clients) {
      if ('navigate' in c) { c.navigate(url); c.focus(); return; }
    }
    self.clients.openWindow(url);
  }));
});
