const CACHE_NAME = 'local-project-console-remote-v33';
const STATIC_ASSETS = [
  '/remote',
  '/remote.css?v=27',
  '/remote.js?v=28',
  '/manifest.webmanifest',
  '/assets/project-console-icon.png',
  '/assets/vendor/qrcode.min.js',
  '/assets/vendor/jsQR.js?v=1',
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))
    ))
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.pathname.startsWith('/api/')) return;
  event.respondWith(
    fetch(event.request, { cache: 'no-store' }).then(response => {
      if (response.ok && url.origin === location.origin) {
        const copy = response.clone();
        caches.open(CACHE_NAME).then(cache => cache.put(event.request, copy));
      }
      return response;
    }).catch(() => caches.match(event.request))
  );
});
