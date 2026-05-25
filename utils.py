#CACHE AVOID DB READS
import threading
import time
import firebase
from db import cakes, reviews, orders, custom_cake_price

_cache = {}
_lock = threading.Lock()
CACHE_TTL = 43200  # 12 hours

# ── Core cache helpers ──
def get_cache(key):
    entry = _cache.get(key)
    if entry and (time.time() - entry["timestamp"]) < CACHE_TTL:
        return entry["data"]
    return None

def set_cache(key, data):
    _cache[key] = {"data": data, "timestamp": time.time()}

def invalidate_cache(*keys):
    for key in keys:
        _cache.pop(key, None)

# ── Cached fetchers ──
def get_all_cakes():
    cached = get_cache("all_cakes")
    if cached:
        print("✅ CACHE HIT — cakes")
        return cached
    with _lock:
        cached = get_cache("all_cakes")
        if cached:
            return cached
        print("🔥 FIRESTORE READ — cakes")
        result = []
        for doc in cakes.stream():
            d = doc.to_dict()
            d["id"] = doc.id
            result.append(d)
        set_cache("all_cakes", result)
        return result

def get_custom_prices():
    cached = get_cache("custom_prices")
    if cached:
        print("✅ CACHE HIT — custom prices")
        return cached
    with _lock:
        cached = get_cache("custom_prices")
        if cached:
            return cached
        print("🔥 FIRESTORE READ — custom prices")
        result = {
            "icing":   custom_cake_price.document("icing").get().to_dict()   or {},
            "size":    custom_cake_price.document("size").get().to_dict()     or {},
            "layers":  custom_cake_price.document("layers").get().to_dict()   or {},
            "toppers": custom_cake_price.document("toppers").get().to_dict()  or {},
            "addons":  custom_cake_price.document("addons").get().to_dict()   or {},
        }
        set_cache("custom_prices", result)
        return result

def get_all_reviews():
    cached = get_cache("all_reviews")
    if cached:
        print("✅ CACHE HIT — reviews")
        return cached
    with _lock:
        cached = get_cache("all_reviews")
        if cached:
            return cached
        print("🔥 FIRESTORE READ — reviews")
        result = []
        for doc in reviews.where("is_visible", "==", True).stream():
            result.append(doc.to_dict())
        set_cache("all_reviews", result)
        return result

def get_order_counts():
    cached = get_cache("order_counts")
    if cached:
        print("✅ CACHE HIT — order counts")
        return cached
    with _lock:
        cached = get_cache("order_counts")
        if cached:
            return cached
        print("🔥 FIRESTORE READ — order counts")
        order_counts = {}
        for doc in orders.stream():
            for item in doc.to_dict().get("selected_items", []):
                cake_id  = item.get("cake_id")
                quantity = int(item.get("quantity", 1))
                if cake_id:
                    order_counts[cake_id] = order_counts.get(cake_id, 0) + quantity
        set_cache("order_counts", order_counts)
        return order_counts
    
def get_locked_dates_cached():
    cached = get_cache("locked_dates")
    if cached:
        print("✅ CACHE HIT — locked dates")
        return cached

    print("🔥 FIRESTORE READ — locked dates")
    from db import locked_dates_ref
    docs  = locked_dates_ref.stream()
    dates = {}
    for doc in docs:
        data = doc.to_dict()
        dates[doc.id] = {
            "reason":       data.get("reason", "Unavailable"),
            "lock_custom":  data.get("lock_custom", False),
            "lock_premade": data.get("lock_premade", False)
        }
    set_cache("locked_dates", dates)
    return dates


def get_completed_cancelled_orders():
    cached = get_cache("completed_cancelled_orders")
    if cached:
        print("✅ CACHE HIT — completed/cancelled orders")
        return cached
    with _lock:
        cached = get_cache("completed_cancelled_orders")
        if cached:
            return cached
        print("🔥 FIRESTORE READ — completed/cancelled orders")
        result = []
        for doc in orders.where("status", "in", ["Completed", "Cancelled"]).stream():
            d = doc.to_dict()
            d["id"] = doc.id
            result.append(d)
        set_cache("completed_cancelled_orders", result)
        return result