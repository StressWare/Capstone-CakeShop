const CACHE_NAME = 'ms-brave-cakes-v1';

const STATIC_ASSETS = [
    '/',
    '/cakes',
    '/static/css/customer_dashboard.css',
    '/static/css/cakes.css',
    '/static/css/chatbot.css',
    '/static/img/logo.png',
];

// Install — cache static assets
self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => {
            return cache.addAll(STATIC_ASSETS).catch(err => {
                console.warn('Some assets failed to cache:', err);
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

// Fetch — network first, fallback to cache
self.addEventListener('fetch', event => {
    // Skip non-GET and browser extension requests
    if (event.request.method !== 'GET') return;
    if (!event.request.url.startsWith(self.location.origin)) return;

    // Skip admin, auth, and API routes — always go to network
    const url = new URL(event.request.url);
    const skipPaths = ['/admin', '/verify-token', '/place-order', '/payment', '/send-message', '/cart/add', '/cart/remove', '/order'];
    if (skipPaths.some(path => url.pathname.startsWith(path))) return;

    event.respondWith(
        fetch(event.request)
            .then(response => {
                // Cache successful responses for static assets
                if (response.ok && (
                    url.pathname.startsWith('/static/') ||
                    url.pathname === '/' ||
                    url.pathname === '/cakes'
                )) {
                    const cloned = response.clone();
                    caches.open(CACHE_NAME).then(cache => cache.put(event.request, cloned));
                }
                return response;
            })
            .catch(() => {
                // Fallback to cache when offline
                return caches.match(event.request).then(cached => {
                    if (cached) return cached;
                    // Offline fallback for HTML pages
                    if (event.request.headers.get('accept').includes('text/html')) {
                        return caches.match('/');
                    }
                })
            })
    );
});