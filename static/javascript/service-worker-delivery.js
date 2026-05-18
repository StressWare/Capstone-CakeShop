
const CACHE_NAME = 'brave-delivery-v1';

// Static assets to pre-cache on install
const STATIC_ASSETS = [
    '/static/img/logo.png',
    'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
    'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
];

// Install — pre-cache static assets
self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => {
            return cache.addAll(STATIC_ASSETS).catch(err => {
                console.warn('Some delivery assets failed to cache:', err);
            });
        })
    );
    self.skipWaiting();
});

// Activate — clean up old caches
self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(
                keys
                    .filter(key => key !== CACHE_NAME)
                    .map(key => caches.delete(key))
            )
        )
    );
    self.clients.claim();
});

// Fetch — Cache First strategy
self.addEventListener('fetch', event => {
    if (event.request.method !== 'GET') {
        event.respondWith(fetch(event.request));
        return;
    }
    const url = new URL(event.request.url);

    // Cache map tiles from OpenStreetMap
    if (url.hostname.includes('tile.openstreetmap.org')) {
        event.respondWith(
            caches.match(event.request).then(cached => {
                if (cached) return cached;
                return fetch(event.request).then(response => {
                    if (response.ok) {
                        const cloned = response.clone();
                        caches.open(CACHE_NAME).then(cache => cache.put(event.request, cloned));
                    }
                    return response;
                }).catch(() => cached);
            })
        );
        return;
    }

    // Cache Leaflet from CDN
    if (url.hostname.includes('unpkg.com')) {
        event.respondWith(
            caches.match(event.request).then(cached => {
                return cached || fetch(event.request).then(response => {
                    if (response.ok) {
                        const cloned = response.clone();
                        caches.open(CACHE_NAME).then(cache => cache.put(event.request, cloned));
                    }
                    return response;
                });
            })
        );
        return;
    }

    // Cache delivery page itself — Cache First
    if (url.pathname.startsWith('/delivery/')) {
        event.respondWith(
            caches.match(event.request).then(cached => {
                // Try network first to get fresh data, fallback to cache
                return fetch(event.request).then(response => {
                    if (response.ok) {
                        const cloned = response.clone();
                        caches.open(CACHE_NAME).then(cache => cache.put(event.request, cloned));
                    }
                    return response;
                }).catch(() => {
                    // Offline — return cached version
                    if (cached) return cached;
                    // No cache either — show offline message
                    return new Response(`
                        <!DOCTYPE html>
                        <html>
                        <head>
                            <meta charset="UTF-8">
                            <meta name="viewport" content="width=device-width, initial-scale=1.0">
                            <title>Offline — Delivery</title>
                            <style>
                                * { margin: 0; padding: 0; box-sizing: border-box; }
                                body {
                                    font-family: 'Segoe UI', sans-serif;
                                    background: #f9f0f5;
                                    display: flex;
                                    align-items: center;
                                    justify-content: center;
                                    min-height: 100vh;
                                    text-align: center;
                                    padding: 20px;
                                }
                                .container {
                                    background: white;
                                    border-radius: 16px;
                                    padding: 40px 30px;
                                    box-shadow: 0 4px 24px rgba(214,51,132,0.12);
                                    max-width: 360px;
                                }
                                .icon { font-size: 3.5rem; margin-bottom: 16px; }
                                h2 { color: #a0205e; font-size: 1.3rem; margin-bottom: 10px; }
                                p { color: #888; font-size: 0.88rem; line-height: 1.6; margin-bottom: 20px; }
                                .tip {
                                    background: #fde8f1;
                                    border-radius: 10px;
                                    padding: 12px;
                                    font-size: 0.82rem;
                                    color: #a0205e;
                                }
                                button {
                                    margin-top: 20px;
                                    background: linear-gradient(135deg, #d63384, #a0205e);
                                    color: white;
                                    border: none;
                                    padding: 12px 24px;
                                    border-radius: 10px;
                                    font-size: 0.9rem;
                                    font-weight: 600;
                                    cursor: pointer;
                                    width: 100%;
                                }
                            </style>
                        </head>
                        <body>
                            <div class="container">
                                <div class="icon">📡</div>
                                <h2>No Connection</h2>
                                <p>You need internet to load this delivery page for the first time.</p>
                                <div class="tip">
                                    💡 Tip: Open this link while connected to save it for offline use.
                                </div>
                                <button onclick="window.location.reload()">Try Again</button>
                            </div>
                        </body>
                        </html>
                    `, {
                        headers: { 'Content-Type': 'text/html' }
                    });
                });
            })
        );
        return;
    }

    // Static assets — cache first
    if (url.pathname.startsWith('/static/')) {
        event.respondWith(
            caches.match(event.request).then(cached => {
                return cached || fetch(event.request).then(response => {
                    if (response.ok) {
                        const cloned = response.clone();
                        caches.open(CACHE_NAME).then(cache => cache.put(event.request, cloned));
                    }
                    return response;
                });
            })
        );
        return;
    }

    // Default — network only
    event.respondWith(fetch(event.request));
});