// firebase-messaging-sw.js

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

// Handle background push notifications
messaging.onBackgroundMessage((payload) => {
    console.log('[FCM SW] Background message received:', payload);

    const { title, body, icon } = payload.notification;

    self.registration.showNotification(title, {
        body: body,
        icon: icon || '/static/img/logo.png',
        badge: '/static/img/logo.png',
        tag: 'delivery-notify',        // replaces previous if still showing
        renotify: true,
        data: payload.data || {}
    });
});

// Notification click → open admin orders page
self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    event.waitUntil(
        clients.openWindow('/admin/orders')
    );
});