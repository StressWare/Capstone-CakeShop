/* ============================================================
   pos-offline-queue.js
   Save as: static/javascript/pos-offline-queue.js
   Include in admin_pos.html BEFORE your existing inline <script>:
       <script src="{{ url_for('static', filename='javascript/pos-offline-queue.js') }}"></script>
   ============================================================ */

(function () {
  const DB_NAME    = 'pos_offline_db';
  const DB_VERSION = 1;
  const STORE      = 'pending_orders';

  function openDB() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(STORE)) {
          db.createObjectStore(STORE, { keyPath: 'idempotency_key' });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror   = () => reject(req.error);
    });
  }

  async function queueOrder(orderPayload) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite');
      tx.objectStore(STORE).put(orderPayload);
      tx.oncomplete = () => resolve();
      tx.onerror    = () => reject(tx.error);
    });
  }

  async function getQueuedOrders() {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readonly');
      const req = tx.objectStore(STORE).getAll();
      req.onsuccess = () => resolve(req.result);
      req.onerror   = () => reject(req.error);
    });
  }

  async function removeQueuedOrder(idempotency_key) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite');
      tx.objectStore(STORE).delete(idempotency_key);
      tx.oncomplete = () => resolve();
      tx.onerror    = () => reject(tx.error);
    });
  }

  function uuid() {
    // Good enough for an idempotency key — doesn't need to be cryptographically strong
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      const r = (Math.random() * 16) | 0;
      const v = c === 'x' ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  function csrfHeaders() {
    return {
      'Content-Type': 'application/json',
      'X-CSRFToken': window.CSRF_TOKEN || '',
    };
  }

  // ── Submit an order: try the network first, fall back to the local queue ──
  // Distinguishes a genuine network failure (fetch throws) — which we queue and
  // retry — from a real server-side rejection (fetch resolves, but !res.ok) —
  // which we surface immediately, since silently retrying an invalid request
  // (bad CSRF token, validation error, etc.) forever will never succeed.
  async function submitOrder(orderPayload) {
    orderPayload.idempotency_key = orderPayload.idempotency_key || uuid();

    let res;
    try {
      res = await fetch('/pos/order', {
        method: 'POST',
        headers: csrfHeaders(),
        body: JSON.stringify(orderPayload),
      });
    } catch (err) {
      // fetch() itself threw — genuinely unreachable (offline, DNS fail, etc.)
      await queueOrder(orderPayload);
      return { synced: false, queued: true };
    }

    if (res.ok) {
      const data = await res.json();
      return { synced: true, data };
    }

    // We got a real response, just not a success — don't silently swallow this.
    let errBody = null;
    try { errBody = await res.json(); } catch (e) { /* not JSON */ }

    if (res.status >= 500) {
      // Server-side outage but reachable — safe to treat like offline and retry later
      await queueOrder(orderPayload);
      return { synced: false, queued: true };
    }

    // 4xx (e.g. csrf_expired, validation) — retrying verbatim won't help.
    // Still don't lose the sale: queue it, but mark it as needing attention.
    orderPayload.sync_error = (errBody && errBody.message) || ('HTTP ' + res.status);
    await queueOrder(orderPayload);
    return { synced: false, queued: true, error: orderPayload.sync_error };
  }

  // ── Replay everything in the queue once we're back online ──
  let syncing = false;
  async function syncQueuedOrders() {
    if (syncing) return;
    syncing = true;
    try {
      const queued = await getQueuedOrders();
      for (const order of queued) {
        let res;
        try {
          res = await fetch('/pos/order', {
            method: 'POST',
            headers: csrfHeaders(),
            body: JSON.stringify(order),
          });
        } catch (err) {
          // still genuinely offline — stop this pass, try again later
          break;
        }

        if (res.ok) {
          const data = await res.json();
          await removeQueuedOrder(order.idempotency_key);
          window.dispatchEvent(new CustomEvent('pos-order-synced', { detail: data }));
        } else if (res.status >= 500) {
          // server still down — leave queued, try again next pass
          break;
        } else {
          // real rejection (e.g. csrf_expired) — leave queued but flag it loudly
          // instead of retrying every 30s forever
          let errBody = null;
          try { errBody = await res.json(); } catch (e) {}
          window.dispatchEvent(new CustomEvent('pos-order-sync-failed', {
            detail: { order, status: res.status, error: errBody }
          }));
        }
      }
    } finally {
      syncing = false;
    }
  }

  window.addEventListener('online', syncQueuedOrders);
  // Backup poll in case the browser's `online` event doesn't fire reliably
  setInterval(() => {
    if (navigator.onLine) syncQueuedOrders();
  }, 30000);
  // Try once on page load too, in case orders were queued in a previous session
  document.addEventListener('DOMContentLoaded', syncQueuedOrders);

  // Expose to the rest of the POS page
  window.POSOfflineQueue = {
    submitOrder,
    getQueuedOrders,
    syncQueuedOrders,
  };
})();