import os
import cloudinary
import cloudinary.uploader
from datetime import datetime, timedelta, timezone
from flask import session, request, current_app

PH_TZ = timezone(timedelta(hours=8))

# ================================================================
# CLOUDINARY CONFIG
# ================================================================
cloudinary.config(
    cloud_name = os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key    = os.getenv('CLOUDINARY_API_KEY'),
    api_secret = os.getenv('CLOUDINARY_API_SECRET')
)

# ================================================================
# CONSTANTS
# ================================================================
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'heic'}

# ================================================================
# FAQ DATA
# ================================================================
FAQ = {
    "how to order": "📝 How to order:\n1. Click 'Order Now'\n2. Pick your cake, flavor & design\n3. Choose delivery date & time\n4. Fill in details & pay\n\nWe'll confirm your order shortly! ✅",
    "delivery": "🚚 Delivery:\n📍 Metro Iloilo & nearby provinces\n💰 Delivery fee: ₱50\n📅 Pick your delivery date & time at checkout\n\n🎂 Premade — delivered on your chosen date\n🎨 Customized — delivered once production is done",
    "pickup": "🏪 Pickup at Shop:\n📍 R Mapa Street, Mandurriao, Iloilo City\n✅ Free — no delivery fee!\n📅 Pick your preferred pickup date & time at checkout\n\n🎂 Premade — ready on your chosen date\n🎨 Customized — ready once production is done",
    "customization": "🎨 We offer full customization!\n\n🍰 Flavors: Vanilla, Chocolate, Red Velvet, Ube, Strawberry & more\n🧁 Frosting: Buttercream, Cream Cheese, Ganache\n🎂 Custom designs, messages & themes\n👶 Sugar-free, dairy-free, vegan available\n\n💰 50% downpayment required\n❌ No cancellation once confirmed",
    "payment methods": "💳 We accept:\n💵 Cash on Delivery (COD)\n📱 GCash & PayMaya\n🏦 Bank Transfer (BPI, BDO, Metrobank)\n💳 Debit/Credit Card\n\nWe'll send payment details after confirmation.",
    "return policy": "🔄 Refund Policy:\n✅ Refund only if cake is damaged or wrong upon delivery\n🕐 Report within 24 hours with photos\n💰 Full refund or replacement — your choice\n\n❌ Baked goods are non-returnable",
    "pricing": "💰 Prices vary by size, design & flavor.\n\nBrowse our cakes on the website or click 'Chat with Owner' for a custom quote.\n\n🎂 Customized cakes need 50% downpayment to proceed.",
    "cancellation": "❌ No cancellation once order is accepted or confirmed.\n\n💰 Downpayment is non-refundable.\n\nIssue on our end? Full refund guaranteed.\nFor concerns → 'Chat with Owner'",
    "minimum order": "🎂 No minimum! You can order just one cake.\n\nFor bulk/events, chat with the owner for special pricing.",
    "location": "📍 R Mapa Street, Mandurriao, Iloilo City\n📱 0956 350 4486\n💬 Facebook: Mrs. Brave's",
    "greeting": "👋 Hi! Welcome to Ms. Brave Cake Shop! 🎂\n\nAsk me about ordering, delivery, customization, payment, or anything else!\n\nOr click 'Chat with Owner' for personal help.",
    "downpayment": "💰 Customized cakes require 50% downpayment before production starts.\n\nAccepted: GCash, PayMaya, Bank Transfer (BPI, BDO, Metrobank)\n\nWe'll send payment details after confirming your order.",
    "order status": "📦 Check your order:\n1. Go to 'My Profile'\n2. Click 'My Orders'\n\nYou'll also get notifications for updates.\nFor concerns → 'Chat with Owner'",
    "default": "😊 I'm not sure about that. Try the FAQ buttons above or click 'Chat with Owner' for help!",
}

FAQ_KEYWORDS = {
    "how to order": [
        "order", "how to", "paano", "mag-order", "purchase", "buy", "step",
        "pano", "paano mag", "gusto ko", "bibili", "bili", "i-order",
        "place order", "mag order", "how do i order", "pwede mag order"
    ],
    "delivery": [
        "delivery", "deliv", "deliver", "shipping", "ship", "hatid", "mahatid",
        "dating", "kelan", "magkano delivery", "delivery fee", "bayad delivery",
        "san-o", "kadugay", "dugay", "padala", "send", "courier", "ipadala"
    ],

    "pickup": [
        "pickup", "pick up", "kukunin", "kunin", "fetch", "self pickup",
        "pick up sa shop", "pupunta", "iikot", "collect", "retrieve",
        "pwede kunin", "pwede mag pickup", "same day pickup"
    ],
    "customization": [
        "custom", "flavor", "flavour", "design", "frosting", "vegan",
        "sugar-free", "dairy", "personali", "theme", "lasa", "gusto",
        "pwede ba", "maaari", "iba", "special", "request", "customize",
        "ilagay", "sulat", "message sa cake", "anong lasa", "anong flavor",
        "mayroon ba", "meron ba", "available ba"
    ],
    "payment methods": [
        "payment", "pay", "gcash", "maya", "cod", "cash", "bank", "transfer",
        "bayad", "magbayad", "paano magbayad", "pwede gcash", "pwede cod",
        "online payment", "credit card", "debit", "bayaran", "pano magbayad",
        "payment method", "load", "paymaya", "bdo", "bpi", "metrobank",
        "pila", "magkano bayad"
    ],
    "return policy": [
        "return", "refund", "damage", "wrong", "incorrect", "issue", "problem",
        "broken", "sira", "mali", "ibalik", "irefund", "pera", "balik pera",
        "hindi tama", "nasira", "may problema", "complaint", "reklamo",
        "di ok", "ayaw", "palitan", "replace", "replacement"
    ],
    "pricing": [
        "price", "pricing", "magkano", "how much", "presyo", "halaga",
        "cost", "quote", "rate", "pila", "tag-pila", "mahal", "mura"
    ],
    "cancellation": [
        "cancel", "cancellation", "kanselahin", "bawiin", "hindi na",
        "ayaw na", "di na tuloy", "withdraw", "back out", "icancel",
        "cancel order", "cancel my order", "pwede i-cancel", "pwede cancel",
        "can i cancel", "cancel ba", "icancel order", "bawiin order"
    ],
    "order status": [
        "status", "where is my order", "tracking", "nasaan", "na deliver na ba",
        "naihatid na", "update", "my order", "order ko", "ano na order ko",
        "kailan dating", "delivered na ba"
    ],
    "minimum order": [
        "minimum", "minimum order", "smallest", "isang cake", "one cake",
        "isa lang", "pwede isang", "maliit na order", "small order"
    ],
    "location": [
        "location", "address", "saan", "nasaan kayo", "where are you",
        "where is", "directions", "how to get", "lugar", "shop"
    ],
    "greeting": [
        "hi", "hello", "hey", "good morning", "good afternoon", "good evening",
        "kumusta", "musta", "sup", "yo", "helo", "magandang"
    ],
    "downpayment": [
        "downpayment", "down payment", "deposit", "50%", "advance payment",
        "bayad muna", "partial payment", "half payment", "dp", "paunang bayad"
    ],
}
# ================================================================
# HELPERS
# ================================================================
def log_admin_action(action, target, category="general"):
    from db import admin_logs
    try:
        admin_logs.add({
            "action":     action,
            "target":     target,
            "category":   category,
            "admin_name": session["user"]["name"],
            "ip_address": request.remote_addr,
            "timestamp":  datetime.now(PH_TZ)
        })
    except Exception:
        current_app.logger.exception("[LOG ERROR] Failed to write admin log")

def get_faq_response(user_message):
    user_message_lower = user_message.lower()
    
    best_match = None
    best_score = 0
    
    for faq_key, keywords in FAQ_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in user_message_lower)
        if score > best_score:
            best_score = score
            best_match = faq_key
    
    if best_match and best_score > 0:
        return FAQ[best_match]
    
    return FAQ.get("default", "I'm not sure. Please contact us directly!")

def save_uploaded_image(file, upload_type):
    parts = file.filename.rsplit('.', 1)
    if len(parts) < 2 or not parts[1]:
        return None
    ext = parts[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return None
    file.seek(0, 2)
    file_size = file.tell()
    file.seek(0)
    if file_size > 2 * 1024 * 1024:
        return None
    folder = 'cake_shop/cakes' if upload_type == 'cake' else 'cake_shop/orders'
    try:
        result = cloudinary.uploader.upload(file, folder=folder, resource_type='image')
        return result['secure_url']
    except Exception:
        current_app.logger.exception("Cloudinary upload error")
        return None
    
def delete_uploaded_image(image_url):
    if image_url and 'cloudinary.com' in image_url:
        public_id = '/'.join(image_url.split('/')[-3:]).rsplit('.', 1)[0]
        try:
            cloudinary.uploader.destroy(public_id)
        except Exception:
            current_app.logger.exception("Cloudinary delete error")

def convert_timestamps(order):
    for field in ['created_at', 'delivery_date']:
        val = order.get(field)
        if isinstance(val, str):
            val = datetime.fromisoformat(val)
        if isinstance(val, datetime):
            if val.tzinfo is None:
                val = val.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
            else:
                val = val.astimezone(PH_TZ)
        order[field] = val
    return order

def calculate_order_total(order):
    if order.get("order_type") == "premade" and order.get("selected_items"):
        return sum(
            float(i.get("subtotal", float(i.get("price", 0)) * int(i.get("quantity", 1))))
            for i in order["selected_items"]
        )
    return float(order.get("amount", 0) or 0)

def _today_range():
    now = datetime.now(PH_TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end

def safe_float(value):
    try:
        return float(value) if value else None
    except (ValueError, TypeError):
        return None

def handle_loyalty_stamp(users_ref, user_id, order_type, selected_items, cakes_ref):
    try:
        earns_stamps = 0

        if order_type == 'custom':
            earns_stamps = 2
        elif order_type == 'premade' and selected_items:
            for item in selected_items:
                cake_doc = cakes_ref.document(item.get('cake_id', '')).get()
                if cake_doc.exists:
                    category = cake_doc.to_dict().get('category', '')
                    if category == 'Cake':
                        earns_stamps = 1
                        break

        if earns_stamps == 0:
            return

        user_ref  = users_ref.document(user_id)
        user_data = user_ref.get().to_dict() or {}

        old_stamps        = int(user_data.get('loyalty_stamps', 0))
        loyalty_unclaimed = user_data.get('loyalty_unclaimed', None)
        new_stamps        = old_stamps + earns_stamps

        update = {}

        if new_stamps >= 10:
            update['loyalty_stamps']    = new_stamps - 10  # carry overshoot
            update['loyalty_unclaimed'] = '15'
        elif old_stamps < 5 <= new_stamps and not loyalty_unclaimed:
            update['loyalty_stamps']    = new_stamps
            update['loyalty_unclaimed'] = '10'
        else:
            update['loyalty_stamps'] = new_stamps

        user_ref.update(update)

    except Exception:
        import logging
        logging.getLogger(__name__).exception("[LOYALTY] Failed to handle loyalty stamp")