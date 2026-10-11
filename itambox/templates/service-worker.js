{% load static %}
const CACHE_NAME = 'itambox-pwa-cache-v{{ settings.VERSION }}';
const OFFLINE_URL = '/offline/';

// Core assets to pre-cache on service worker installation
const PRECACHE_ASSETS = [
  OFFLINE_URL,
  '{% static 'dist/itambox.css' %}?v={{ settings.VERSION }}',
  '{% static 'dist/itambox.js' %}?v={{ settings.VERSION }}',
  '/static/dist/vendor/bootstrap.bundle.min.js',
  '/static/dist/vendor/htmx.min.js',
  '/static/pwa/icon-192.png',
  '/static/pwa/icon-512.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((cacheName) => {
          if (cacheName !== CACHE_NAME) {
            return caches.delete(cacheName);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  // Only handle GET requests
  if (event.request.method !== 'GET') {
    return;
  }

  // Handle page navigation requests (HTML pages)
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request)
        .catch(() => {
          // If network fetch fails (offline), return cached offline page
          return caches.match(OFFLINE_URL);
        })
    );
    return;
  }

  // Cache-first strategy for static assets under the /static/ path
  const url = new URL(event.request.url);
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(event.request)
        .then((cachedResponse) => {
          if (cachedResponse) {
            return cachedResponse;
          }
          return fetch(event.request).then((response) => {
            // Guard clause for invalid responses
            if (!response || response.status !== 200 || response.type !== 'basic') {
              return response;
            }
            // Clone and cache the newly requested static asset
            const responseToCache = response.clone();
            caches.open(CACHE_NAME).then((cache) => {
              cache.put(event.request, responseToCache);
            });
            return response;
          });
        })
    );
  }
});
