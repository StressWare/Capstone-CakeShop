# CACHE — AVOID DB READS
import threading
import time
import firebase 
from db import cakes, reviews, orders, custom_cake_price, loyalty_gifts

# After
_cache = {}
_lock = threading.Lock()

CACHE_TTL = 43200 #12hrs

def get_cache(key):
    entry = _cache.get(key)
    if entry and (time.time() - entry["timestamp"]) < CACHE_TTL:
        return entry["data"]
    return None

def set_cache(key, data):
    with _lock:
        _cache[key] = {"data": data, "timestamp": time.time()}

def invalidate_cache(*keys):
    with _lock:
        for key in keys:
            _cache.pop(key, None)


# ── Internal: fetch-or-cache helper ──

def _fetch_or_cache(key, fetch_fn):
    # Fast path — no lock
    cached = get_cache(key)
    if cached is not None:
        print(f"✅ CACHE HIT — {key}")
        return cached

    # Slow path — acquire lock, check again, then fetch
    with _lock:
        cached = get_cache(key)
        if cached is not None:
            return cached
        print(f"🔥 FIRESTORE READ — {key}")
        result = fetch_fn()
        _cache[key] = {"data": result, "timestamp": time.time()}
        return result


# ── Cached fetchers ──

def get_all_cakes():
    def fetch():
        result = []
        for doc in cakes.stream():
            d = doc.to_dict()
            d["id"] = doc.id
            result.append(d)
        return result
    return _fetch_or_cache("all_cakes", fetch)


def get_custom_prices():
    def fetch():
        return {
            "icing":   custom_cake_price.document("icing").get().to_dict()    or {},
            "size":    custom_cake_price.document("size").get().to_dict()      or {},
            "layers":  custom_cake_price.document("layers").get().to_dict()    or {},
            "toppers": custom_cake_price.document("toppers").get().to_dict()   or {},
            "addons":  custom_cake_price.document("addons").get().to_dict()    or {},
        }
    return _fetch_or_cache("custom_prices", fetch)


def get_loyalty_gifts():
    def fetch():
        docs = list(loyalty_gifts.limit(1).stream())
        if docs:
            return docs[0].to_dict()
        default = {
            "small": ["Cupcake", "Coffee", "Pastry"],
            "big":   ["Bento Cake", "Slice Cake", "Drinks Bundle"],
        }
        loyalty_gifts.add(default)
        return default
    return _fetch_or_cache("loyalty_gifts", fetch)


def get_all_reviews():
    def fetch():
        return [
            doc.to_dict()
            for doc in reviews.where("is_visible", "==", True).stream()
        ]
    return _fetch_or_cache("all_reviews", fetch)


def get_order_counts():
    def fetch():
        counts = {}
        for doc in orders.stream():
            for item in doc.to_dict().get("selected_items", []):
                cake_id  = item.get("cake_id")
                quantity = int(item.get("quantity", 1))
                if cake_id:
                    counts[cake_id] = counts.get(cake_id, 0) + quantity
        return counts
    return _fetch_or_cache("order_counts", fetch)


def get_locked_dates_cached():
    def fetch():
        from db import locked_dates_ref
        return {
            doc.id: {
                "reason":       doc.to_dict().get("reason", "Unavailable"),
                "lock_custom":  doc.to_dict().get("lock_custom", False),
                "lock_premade": doc.to_dict().get("lock_premade", False),
            }
            for doc in locked_dates_ref.stream()
        }
    return _fetch_or_cache("locked_dates", fetch)


def get_completed_cancelled_orders():
    def fetch():
        result = []
        for doc in (
            orders
            .where("status", "in", ["Completed", "Cancelled"])
            .order_by("created_at", direction="DESCENDING")
            .stream()
        ):
            d = doc.to_dict()
            d["id"] = doc.id
            result.append(d)
        return result
    return _fetch_or_cache("completed_cancelled_orders", fetch)


def get_converted_consultations():
    def fetch():
        from db import conversations
        result = []
        for doc in (
            conversations
            .where("is_consultation", "==", True)
            .where("status", "==", "converted")
            .stream()
        ):
            d = doc.to_dict()
            d["conversation_id"] = doc.id
            result.append(d)
        return result
    return _fetch_or_cache("converted_consultations", fetch)