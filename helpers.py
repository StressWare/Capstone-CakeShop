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
    "how to order": "📝 To place an order with Ms. Brave Cake Shop:\n\n1. Click 'Order Now' on our homepage\n2. Select your cake design and flavor\n3. Choose delivery date and time\n4. Fill in recipient details\n5. Review and confirm your order\n6. Choose payment method\n\nThat's it! We'll confirm your order shortly.",
    "delivery time": "🚚 Our delivery times:\n\n⏱️ Standard Delivery: 2-3 business days\n⚡ Rush Delivery: 24 hours (available for ₱150 extra)\n\n📍 Delivery areas: Metro and nearby provinces\n🎁 Free delivery for orders ₱2000 and above\n\nDelivery is available 10 AM - 6 PM daily.",
    "customization": "🎨 Yes! We offer full customization:\n\n🍰 Flavors: Vanilla, Chocolate, Red Velvet, Ube, Strawberry, and more\n🧁 Frosting: Buttercream, Cream Cheese, Chocolate Ganache\n🎂 Design: Custom designs, personalized messages, themed decorations\n👶 Special requests: Sugar-free, dairy-free, vegan options available\n\nPlease mention your preferences in the order notes!",
    "payment methods": "💳 We accept multiple payment methods:\n\n💵 Cash on Delivery (COD)\n📱 GCash & PayMaya\n🏦 Bank Transfer (BPI, BDO, Metrobank)\n💰 Online Payment (Debit/Credit Card)\n\nPayment must be settled before delivery. We send a QR code or bank details after confirmation.",
    "return policy": "🔄 Return & Refund Policy:\n\n❌ Non-returnable items: Baked goods due to perishability\n✅ Refund eligibility: Only if cake is damaged or incorrect upon delivery\n🕐 Timeline: Report issues within 24 hours of delivery\n💰 Refund process: Full refund or replacement (customer's choice)\n\nPlease message us immediately with photos if there's an issue!",
    "default": "😊 I'm not sure about that question. Please click one of the FAQ buttons above or contact the owner directly using the 'Chat with Owner' button. Thank you!"
}
FAQ_KEYWORDS = {
    "how to order": [
        "order", "how to", "paano", "mag-order", "purchase", "buy", "step",
        "pano", "paano mag", "gusto ko", "bibili", "bili", "i-order",
        "place order", "mag order", "how do i order", "pwede mag order"
    ],
    "delivery time": [
        "delivery", "deliv", "deliver", "shipping", "ship", "days", "araw",
        "kailan", "when", "rush", "standard", "hatid", "mahatid", "ilang araw",
        "gaano katagal", "tagal", "dating", "dating ng", "kelan", "free delivery",
        "libre delivery", "magkano delivery", "delivery fee", "bayad delivery"
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
        "payment method", "load", "paymaya", "bdo", "bpi", "metrobank"
    ],
    "return policy": [
        "return", "refund", "damage", "wrong", "incorrect", "issue", "problem",
        "broken", "sira", "mali", "ibalik", "irefund", "pera", "balik pera",
        "hindi tama", "nasira", "may problema", "complaint", "reklamo",
        "di ok", "ayaw", "palitan", "replace", "replacement"
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

        stamps           = int(user_data.get('loyalty_stamps', 0)) + earns_stamps
        loyalty_unclaimed = user_data.get('loyalty_unclaimed', None)

        update = {'loyalty_stamps': stamps}

        if stamps >= 10:
            update['loyalty_unclaimed'] = '15'
            update['loyalty_stamps']    = 0
        elif stamps >= 5 and not loyalty_unclaimed:
            update['loyalty_unclaimed'] = '10'

        user_ref.update(update)

    except Exception:
        import logging
        logging.getLogger(__name__).exception("[LOYALTY] Failed to handle loyalty stamp")