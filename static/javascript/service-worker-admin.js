importScripts('https://www.gstatic.com/firebasejs/9.23.0/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/9.23.0/firebase-messaging-compat.js');

firebase.initializeApp({
    apiKey: "AIzaSyAKe10I1uuRRm67YpdUu7ZYgIVHnlbLdKY",
    authDomain: "cakeshop-2faf4.firebaseapp.com",
    projectId: "cakeshop-2faf4",
    storageBucket: "cakeshop-2faf4.firebasestorage.app",
    messagingSenderId: "470246706853",
    appId: "1:470246706853:web:339c4084715b0286b6474c"
});

const messaging = firebase.messaging();

messaging.onBackgroundMessage((payload) => {
    console.log('[FCM SW] Background message:', payload);
    const { title, body } = payload.notification || {};
    const data = payload.data || {};

    const isRush = data.rush === 'true';
    const tag = data.type === 'new_order' ? 'new-order' : 'delivery-notify';

    self.registration.showNotification(title || ' New Order!', {
        body:     body || '',
        icon:     '/static/img/logo.png',
        badge:    '/static/img/logo.png',
        tag:      tag,
        renotify: true,
        vibrate:  isRush ? [200, 100, 200, 100, 200] : [200, 100, 200],
        data:     data
    });
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    event.waitUntil(clients.openWindow('/admin/orders'));
});


const CACHE_NAME = 'brave-admin-v1';

// Only cache static assets — never admin data pages
const STATIC_ASSETS = [
    '/static/css/admin_dashboard.css',
    '/static/css/resposive_admin.css',
    '/static/img/logo.png',
    '/static/img/mrs.brave.jpg',
];

// Install — cache static assets
self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => {
            return cache.addAll(STATIC_ASSETS).catch(err => {
                console.warn('Some admin assets failed to cache:', err);
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

// Fetch strategy
self.addEventListener('fetch', event => {
    if (event.request.method !== 'GET') {
        event.respondWith(fetch(event.request));
        return;
    }
    if (!event.request.url.startsWith(self.location.origin)) return;

    const url = new URL(event.request.url);

    // Always skip admin HTML pages — never cache, always fresh
    const skipPaths = [
        '/admin',
        '/order',
        '/place-order',
        '/payment',
        '/delivery',
        '/verify-token',
        '/send-message',
        '/cart',
        '/paymongo'
    ];

    if (skipPaths.some(path => url.pathname.startsWith(path))) {
        // Network only — if offline, return offline page
        event.respondWith(
            fetch(event.request).catch(() => {
                return new Response(`
                    <!DOCTYPE html>
                    <html>
                    <head>
                        <meta charset="UTF-8">
                        <meta name="viewport" content="width=device-width, initial-scale=1.0">
                        <title>You're Offline — Admin</title>
                        <style>
                            * { margin: 0; padding: 0; box-sizing: border-box; }
                            body {
                                font-family: 'Segoe UI', sans-serif;
                                background: #fde8f1;
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
                            .warning {
                                background: #fff3cd;
                                border: 1.5px solid #ffe082;
                                border-radius: 10px;
                                padding: 12px;
                                font-size: 0.82rem;
                                color: #856404;
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
                            <h2>You're Offline</h2>
                            <p>Admin data requires an internet connection to stay accurate and up to date.</p>
                            <div class="warning">
                                ⚠️ For safety, admin data is never cached. Please reconnect to view orders, sales, and other live data.
                            </div>
                            <button onclick="window.location.reload()">Try Again</button>
                        </div>
                    </body>
                    </html>
                `, {
                    headers: { 'Content-Type': 'text/html' }
                });
            })
        );
        return;
    }

    // For static assets — cache first, fallback to network
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

    // Default — network first
    event.respondWith(fetch(event.request));
});