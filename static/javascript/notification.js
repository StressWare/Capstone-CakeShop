// Notifications System
let unsubscribeNotifications = null;
let isFirstLoad = true;  // ← ADD THIS

function initNotifications() {
    const userIdElement = document.querySelector('[data-user-id]');
    if (!userIdElement) {
        console.log('No data-user-id found on body element');
        return;
    }
    
    const userId = userIdElement.dataset.userId;
    if (!userId) {
        console.log('User ID is empty');
        return;
    }
    
    console.log('Initializing notifications for user:', userId);
    
    const db = firebase.firestore();
    firebase.auth().onAuthStateChanged(user => {
        if (!user) return;
        
        unsubscribeNotifications = db.collection("notifications")
            .where("user_id", "==", userId)
            .orderBy("created_at", "desc")
            .limit(20)
            .onSnapshot((snapshot) => {
                // Get ALL notifications from the snapshot
                const allNotifications = [];
                let unreadCount = 0;
                
                snapshot.forEach((doc) => {
                    const notif = doc.data();
                    notif.id = doc.id;
                    if (!notif.is_read) unreadCount++;
                    allNotifications.push(notif);
                });
                
                // Show toast for newly added notifications only
                if (!isFirstLoad) {
                    snapshot.docChanges().forEach((change) => {
                        if (change.type === 'added') {
                            const notif = change.doc.data();
                            showToastNotification(notif.title, notif.message);
                        }
                    });
                }
                
                isFirstLoad = false;
                
                updateUnreadBadge(unreadCount);
                renderNotifications(allNotifications);
            }, (error) => {
                console.error('Notification listener error:', error);
            });
    });
}

function updateUnreadBadge(count) {
    const badge = document.getElementById('unreadBadge');
    if (!badge) return;
    
    if (count > 0) {
        badge.textContent = count > 99 ? '99+' : count;
        badge.style.display = 'inline';
    } else {
        badge.style.display = 'none';
    }
}

function renderNotifications(notifications) {
    const container = document.getElementById('notificationList');
    if (!container) return;
    
    if (!notifications || notifications.length === 0) {
        container.innerHTML = '<div style="text-align: center; padding: 25px 16px; color: #aaa;"><i class="fas fa-bell-slash"></i><br>No notifications yet</div>';
        return;
    }
    
    let html = '';
    notifications.forEach(notif => {
        const timeAgo = getTimeAgo(notif.created_at?.toDate());
        const unreadStyle = !notif.is_read ? 'background: #fff0f5; border-left: 3px solid #d63384;' : '';
        
        html += `
            <div style="padding: 10px 14px; border-bottom: 1px solid #f5f5f5; position: relative; ${unreadStyle}">
                <div style="cursor: pointer; padding-right: 24px;"
                    onclick="handleNotificationClick('${notif.id}', '${notif.order_id || ''}')">
                    <div style="font-weight: 600; font-size: 0.85rem; color: #333; margin-bottom: 3px;">${escapeHtml(notif.title)}</div>
                    <div style="color: #666; font-size: 0.75rem; margin-bottom: 3px;">${escapeHtml(notif.message)}</div>
                    <div style="font-size: 0.7rem; color: #999;">${timeAgo}</div>
                </div>
                <button onclick="event.stopPropagation(); deleteNotification('${notif.id}')"
                        style="position: absolute; top: 8px; right: 10px; background: none; border: none; color: #ccc; font-size: 0.85rem; cursor: pointer; line-height: 1; padding: 2px 4px; border-radius: 4px;"
                        onmouseover="this.style.color='#d63384'; this.style.background='#fff0f5';"
                        onmouseout="this.style.color='#ccc'; this.style.background='none';"
                        title="Delete notification">✕</button>
            </div>
        `;
    });
    container.innerHTML = html;
}

function getTimeAgo(date) {
    if (!date) return '';
    
    const now = new Date();
    const seconds = Math.floor((now - date) / 1000);
    
    if (seconds < 60) return 'just now';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    if (days < 7) return `${days}d ago`;
    return date.toLocaleDateString();
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

async function handleNotificationClick(notifId, orderId) {
    await markNotificationAsRead(notifId);
    if (orderId) {
        window.location.href = '/customer_dashboard';
    }
}

async function markNotificationAsRead(notifId) {
    try {
        const db = firebase.firestore();
        await db.collection("notifications").doc(notifId).update({ "is_read": true });
    } catch (error) {
        console.error('Error marking notification as read:', error);
    }
}

async function markAllNotificationsAsRead() {
    const userIdElement = document.querySelector('[data-user-id]');
    if (!userIdElement) return;
    
    const userId = userIdElement.dataset.userId;
    if (!userId) return;
    
    try {
        const db = firebase.firestore();
        const snapshot = await db.collection("notifications")
            .where("user_id", "==", userId)
            .where("is_read", "==", false)
            .get();
        
        const batch = db.batch();
        snapshot.forEach(doc => {
            batch.update(doc.ref, { "is_read": true });
        });
        await batch.commit();
    } catch (error) {
        console.error('Error marking all as read:', error);
    }
}

async function deleteNotification(notifId) {
    try {
        const db = firebase.firestore();
        await db.collection("notifications").doc(notifId).delete();
    } catch (error) {
        console.error('Error deleting notification:', error);
    }
}

async function clearReadNotifications() {
    const userIdElement = document.querySelector('[data-user-id]');
    if (!userIdElement) return;

    const userId = userIdElement.dataset.userId;
    if (!userId) return;

    try {
        const db = firebase.firestore();
        const snapshot = await db.collection("notifications")
            .where("user_id", "==", userId)
            .where("is_read", "==", true)
            .get();

        const batch = db.batch();
        snapshot.forEach(doc => {
            batch.delete(doc.ref);
        });
        await batch.commit();
    } catch (error) {
        console.error('Error clearing read notifications:', error);
    }
}

function showToastNotification(title, message) {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.style.position = 'fixed';
        container.style.bottom = '20px';
        container.style.right = '20px';
        container.style.zIndex = '9999';
        document.body.appendChild(container);
    }
    
    const toast = document.createElement('div');
    toast.style.marginBottom = '8px';
    toast.style.animation = 'slideIn 0.3s ease';
    toast.innerHTML = `
        <div style="background: #d63384; color: white; padding: 10px 16px; border-radius: 8px; box-shadow: 0 3px 10px rgba(0,0,0,0.2); min-width: 250px;">
            <div style="font-weight: 600; font-size: 0.85rem; margin-bottom: 3px;">🔔 ${escapeHtml(title)}</div>
            <div style="font-size: 0.75rem; opacity: 0.9;">${escapeHtml(message)}</div>
        </div>
    `;
    
    container.appendChild(toast);
    
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transition = 'opacity 0.3s';
        setTimeout(() => toast.remove(), 300);
    }, 5000);
}

// Add animation CSS
const style = document.createElement('style');
style.textContent = `
    @keyframes slideIn {
        from {
            opacity: 0;
            transform: translateX(100px);
        }
        to {
            opacity: 1;
            transform: translateX(0);
        }
    }
`;
document.head.appendChild(style);

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', function() {
    console.log('DOM loaded, checking for notification bell...');
    
    if (document.getElementById('notificationBellWrapper')) {
        console.log('Notification bell found, initializing...');
        initNotifications();
    } else {
        console.log('No notification bell found on this page');
    }
    
    // Bell click to toggle dropdown
    const bellWrapper = document.getElementById('notificationBellWrapper');
    const dropdown = document.getElementById('notificationDropdown');
    
    if (bellWrapper && dropdown) {
        bellWrapper.addEventListener('click', function(e) {
            e.stopPropagation();
            if (dropdown.style.display === 'block') {
                dropdown.style.display = 'none';
            } else {
                dropdown.style.display = 'block';
            }
        });
    }
    
    // Close dropdown when clicking outside
    document.addEventListener('click', function(e) {
        if (dropdown && !dropdown.contains(e.target) && !bellWrapper.contains(e.target)) {
            dropdown.style.display = 'none';
        }
    });
    
    // Mark all read button
    const markAllBtn = document.getElementById('markAllReadBtn');
    if (markAllBtn) {
        markAllBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            markAllNotificationsAsRead();
        });
    }

    const clearReadBtn = document.getElementById('clearReadBtn');
    if (clearReadBtn) {
        clearReadBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            clearReadNotifications();
        });
    }
});