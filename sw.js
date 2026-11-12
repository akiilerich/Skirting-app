const CACHE_NAME = 'carpentry-app-v5';
const CORE_ASSETS = [
  './',
  './index.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png',
  './architrave/index.html',
  './skirting/index.html',
  './overlay-door/index.html',
  './doors/index.html',
  './doors/data.js',
  './doors/plan-data.js'
];

// The plan drawings are large (about 4 MB each before compression), so they are fetched
// in the background after install and one failing never blocks the app from installing.
const PLAN_ASSETS = [
  'first-floor', 'second-floor', 'third-floor', 'fourth-floor', 'fifth-floor', 'ground-floor', 'lower-ground'
].map((n) => './doors/plans/' + n + '.svg');

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      cache.addAll(CORE_ASSETS).then(() => {
        PLAN_ASSETS.reduce((chain, url) => chain.then(() => cache.add(url).catch(() => {})), Promise.resolve());
      })
    )
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Cache-first for the app shell, falling back to network, and updating the
// cache in the background so the next launch picks up new versions.
self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;

  event.respondWith(
    caches.match(event.request).then((cached) => {
      const network = fetch(event.request)
        .then((response) => {
          if (response && response.status === 200) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          }
          return response;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
