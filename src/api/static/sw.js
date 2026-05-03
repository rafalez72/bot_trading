// Service worker minimalista: cachea el shell, deja la API siempre fresca.
const CACHE = 'copybot-v10-multi-tab';
const SHELL = ['/', '/static/app.js', '/static/style.css', '/manifest.json'];

self.addEventListener('install', (e) => {
    e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
    self.skipWaiting();
});

self.addEventListener('activate', (e) => {
    e.waitUntil(
        caches.keys().then((keys) =>
            Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
        )
    );
    self.clients.claim();
});

self.addEventListener('fetch', (e) => {
    const url = new URL(e.request.url);
    // API: network-first, sin caché
    if (url.pathname.startsWith('/api/')) return;
    // Resto: cache-first con revalidación
    e.respondWith(
        caches.match(e.request).then((cached) => {
            const fetchPromise = fetch(e.request).then((res) => {
                if (res.ok) {
                    const copy = res.clone();
                    caches.open(CACHE).then((c) => c.put(e.request, copy));
                }
                return res;
            }).catch(() => cached);
            return cached || fetchPromise;
        })
    );
});
