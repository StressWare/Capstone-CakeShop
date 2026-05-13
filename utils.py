#CACHE AVOID DB READS
import threading
import time
import firebase
from db import cakes, reviews, orders

_cache = {}
_lock = threading.Lock()
CACHE_TTL = 3600  #1hr

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