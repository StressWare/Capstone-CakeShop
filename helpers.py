import os
import cloudinary
import cloudinary.uploader
from datetime import datetime, timedelta, timezone
from flask import session, request, current_app
from dotenv import load_dotenv
from google import genai
from firebase_admin import messaging as fcm_messaging
from firebase_admin import firestore as admin_fs
import logging
load_dotenv()

logger = logging.getLogger(__name__)
PH_TZ = timezone(timedelta(hours=8))

# CLOUDINARY CONFIG
cloudinary.config(
    cloud_name = os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key    = os.getenv('CLOUDINARY_API_KEY'),
    api_secret = os.getenv('CLOUDINARY_API_SECRET')
)

# CONSTANTS
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'heic'}


# FAQ DATA
FAQ = {
    "how to order": (
        "Ordering is simple:\n\n"
        "1. Browse our available cakes or go to Customize Your Cake.\n"
        "2. Select your preferred cake, size, flavor, and design.\n"
        "3. Choose your delivery or pickup date and time.\n"
        "4. Fill in your delivery details and complete payment.\n\n"
        "Once submitted, we will review and confirm your order. "
        "You will receive a notification once it is confirmed."
    ),

    "delivery": (
        "We deliver within Metro Iloilo and nearby areas.\n\n"
        "Delivery fee starts at P50 (base rate); longer distances may have an additional charge.\n"
        "You can select your preferred delivery date and time at checkout.\n\n"
        "For premade cakes, expect delivery on your chosen date. "
        "For custom cakes, delivery is scheduled once production is complete.\n\n"
        "If you have specific delivery concerns, you may chat with the owner directly."
    ),

    "pickup": (
        "You may pick up your order at our shop at no extra charge.\n\n"
        "Address: R. Mapa Street, Mandurriao, Iloilo City\n"
        "Contact: 0956 350 4486\n\n"
        "Select your preferred pickup date and time at checkout. "
        "We will notify you once your order is ready for pickup."
    ),

    "customization": (
        "We offer full cake customization.\n\n"
        "Use the \"Customize Your Cake\" option to choose your cake size, flavor, frosting, design, "
        "message, and other details. You can also request sugar-free, dairy-free, or vegan options.\n\n"
        "A 50% downpayment is required before production begins, and orders cannot be cancelled once confirmed."
    ),
    "custom consultation": (
        "Yes, you can consult with us first for a customized cake.\n\n"
        "On the \"Customize Your Cake\" form, choose the option to consult first and upload your reference photo, "
        "theme, budget, and event date.\n\n"
        "We will review your details and contact you to discuss the design and price. "
        "You can also chat with the owner if you have more questions."
    ),
    "payment methods": (
        "We accept the following payment methods:\n\n"
        "- Cash on Delivery (COD)\n"
        "- PayMaya\n"
        "- GCash\n"
        "- Debit or Credit Card\n"
        "- QRPH,\n\n"
        "Payment details will be provided after your order is confirmed."
    ),

    "return policy": (
        "All sales are final and baked goods are non-refundable and non-returnable.\n\n"
        "If the cake arrives damaged or clearly incorrect, please contact us within 24 hours "
        "and send photos of the issue. We will review the case and may offer a replacement or "
        "other resolution at our discretion."
    ),

    "pricing": (
        "Prices vary depending on the cake size, design complexity, and flavor.\n\n"
        "You can browse premade cakes and their prices on the website. "
        "For customized cakes, use the \"Customize Your Cake\" option (you may also choose consult first) "
        "so we can review your details and give you a quote."
    ),

    "cancellation": (
        "Orders cannot be cancelled once they have been accepted or confirmed.\n\n"
        "Downpayments are non-refundable.\n\n"
        "If the issue is on our end, a full refund will be processed. "
        "For any concerns, please chat with the owner directly."
    ),

    "minimum order": (
        "There is no minimum order requirement. You may order as little as one cake.\n\n"
        "For bulk orders or event catering, please chat with the owner for special pricing and arrangements."
    ),

    "location": (
        "Our shop is located at:\n\n"
        "R. Mapa Street, Mandurriao, Iloilo City\n\n"
        "Contact: 0956 350 4486\n"
        "Facebook: Mrs. Brave's Cake Shop\n\n"
        "We are open Monday to Saturday, 11:00 AM to 10:00 PM."
    ),

    "greeting": (
        "Hello! I am Brave Bot, the assistant of Mrs. Brave's Cake Shop! \n\n"
        "I can help you with questions about ordering, delivery, customization, payment, and more. "
        "You may also use the quick question buttons below, or click Chat with Owner for personal assistance."
    ),
    "downpayment": (
        "A 50% downpayment is required for all custom cake orders before production begins.\n\n"
        "Accepted payment methods for downpayment:\n"
        "- PayMaya\n"
        "- GCash\n"
        "- Debit or Credit Card\n"
        "- QRPH\n\n"
        "Payment details will be sent to you after your order is confirmed. "
        "Downpayments are non-refundable once production has started."
    ),

    "order status": (
        "To check your order status:\n\n"
        "1. Log in to your account.\n"
        "2. Go to My Profile.\n"
        "3. Click My Orders to view the current status.\n\n"
        "You will also receive notifications whenever your order status is updated. "
        "For urgent concerns, please chat with the owner."
    ),

    "default": (
        "I'm sorry, I could not find an answer to that. "
        "Please try one of the quick question buttons, or click Chat with Owner for direct assistance."
    ),
}


FAQ_KEYWORDS = {
    "how to order": [
        "order", "how to", "purchase", "buy", "step", "place order",
        "how do i order", "how to buy", "i want to order", "how to purchase",
        "paano", "mag-order", "pano", "paano mag", "gusto ko", "bibili",
        "bili", "i-order", "mag order", "pwede mag order", "paano mag-order",
        "gusto mag order", "saan mag order", "pano bumili", "gusto bumili",
        "pwede ba mag order", "mag-order ako", "iorder ko", "paano ba",
        "bibili ako", "order na ako",
        "paano makaorder", "gusto ko magorder", "pwede ba mag-order diri",
        "paano sa inyo mag-order", "mag-order ko", "paano magpaorder",
        "gusto ko magpalit", "pwede mag order diri", "paano ko maorder"
    ],
    "delivery": [
        "delivery", "deliv", "deliver", "shipping", "ship", "send",
        "courier", "delivery fee", "delivery charge", "free delivery",
        "same day delivery", "how long delivery", "where do you deliver",
        "hatid", "mahatid", "dating", "kelan", "magkano delivery",
        "bayad delivery", "padala", "ipadala", "ihatid",
        "pwede i-deliver", "may delivery ba", "magpadala",
        "kelan darating", "kailan darating", "ilang araw",
        "gaano katagal", "deliver ba kayo", "may delivery kayo",
        "magkano pag deliver", "libre ba delivery", "may charge ba",
        "saan kayo nagde-deliver",
        "mahatod", "ihatod", "ipadala ninyo", "san-o mahatod",
        "pila ang delivery", "libre ba ang delivery", "may delivery ba kamo",
        "gaano katagal mahatod", "diin kamo nagahatod", "san-o darating"
    ],
    "pickup": [
        "pickup", "pick up", "fetch", "self pickup", "collect", "retrieve",
        "same day pickup", "no delivery fee", "pick up at shop",
        "kukunin", "kunin", "pick up sa shop", "pupunta", "iikot",
        "pwede kunin", "pwede mag pickup", "kukuha", "pwede pumunta",
        "pupunta ako", "saan kukuha", "ikukuha ko", "mag-pickup ako",
        "pwede bang kunin", "libre ba ang pickup", "walang bayad pickup",
        "pick up lang", "doon na lang kukuha",
        "kuhaon", "kuhaon ko", "pwede ko kuhaon", "ikuha ko",
        "maabot ko", "pupunta ko dira", "san-o ko kuhaon",
        "pwede ba ako mag-pickup", "kuhaon na lang nako"
    ],
    "customization": [
        "custom", "flavor", "flavour", "design", "frosting", "vegan",
        "sugar-free", "dairy", "theme", "special", "request", "customize",
        "personalized", "birthday cake", "themed cake", "custom design",
        "custom message", "cake flavor", "available flavors",
        "personali", "lasa", "gusto", "pwede ba", "maaari", "iba",
        "ilagay", "sulat", "message sa cake", "anong lasa", "anong flavor",
        "mayroon ba", "meron ba", "available ba", "pwede bang i-customize",
        "gusto ko ng custom", "anong available na lasa",
        "pwede bang lagyan ng message", "may theme ba kayo",
        "anong frosting", "pwede bang vegan", "gusto ko ng design",
        "pwede bang lagyan ng pangalan", "anong pwede", "maari bang baguhin",
        "ano ang mga lasa", "pwede ba mag-customize", "gusto ko specialty",
        "pwede ba lagyan og message", "may tema ba kamo",
        "anong available nga lasa", "pwede ba vegan",
        "anu-ano ang frosting", "pwede ba sugar-free",
        "gusto ko special cake", "pwede ba themed cake",
        "anong mga design available", "pwede lagyan ng ngalan",
        "customize your cake", "customized template", "custom form",
        "custom order form", "template for custom", "paano mag custom",
        "paano mag-customize", "custom na cake", "gusto ko custom na cake",
        "customized cake", "customized order", "paano mag pa custom",
    ],
     "custom consultation": [
        "consultation", "consult", "talk first", "discuss design", "design ideas",
        "design consultation", "quote first", "ask first", "before ordering",
        "can i ask first", "can i talk to you", "send my design", "send a peg",
        "show you a picture", "can you suggest", "help with design",
        "consultation", "consult", "talk first", "discuss design", "design ideas",
        "design consultation", "quote first", "ask first", "before ordering",
        "can i ask first", "can i talk to you", "send my design", "send a peg",
        "show you a picture", "can you suggest", "help with design",
        "customized consultation", "consult for custom cake",
        "usap tungkol sa custom", "tanong muna sa custom", "idea para sa custom cake",
        "help with customized cake", "design for customized cake",
        "mag consult", "mag-consult", "pwede mag tanong muna", "tanong muna",
        "usap muna", "kausapin muna", "pwede ba mag usap", "usap tungkol sa design",
        "mag pa quote", "magpa quote", "pa quote muna", "tanong tungkol sa custom",
        "padala ng peg", "padala ng design", "picture muna", "idea sa cake",
        "tulong sa design", "hingi ng suggestion", "magpatulong sa design",
        "pamangkot anay", "pwede mag pamangkot", "istorya anay", "istorya ta anay",
        "mga idea sa design", "pa idea sang cake", "padala sang peg",
        "padala sang picture", "pwede ko anay magpamangkot", "consult anay",
        "pangayo ko idea", "pangayo ko quote"
    ],
    "payment methods": [
        "how to pay", "payment method", "payment option", "payment",
        "gcash", "maya", "paymaya", "cod", "cash", "transfer",
        "online payment", "credit card", "debit", "card", "qrph",
        "accepted payment", "payment accepted", "do you accept gcash",
        "bayad", "magbayad", "paano magbayad", "pwede gcash", "pwede cod",
        "bayaran", "pano magbayad", "load", "paano ba magbayad",
        "pwede bang gcash", "pwede bang maya", "pwede bang card",
        "anong paraan ng bayad", "cash on delivery ba", "may cod ba",
        "online ba bayad", "paano ko babayaran", "accept gcash",
        "tanggap gcash", "tanggap maya", "bayad online",
        "pwede bang bayaran online", "magkano babayaran", "kailan babayaran",
        "paano magbayad", "pila ang bayad", "pwede gcash diri",
        "pwede maya diri", "cash lang ba", "may cod ba kamo",
        "paano ko mabayaran", "anong paraan sang pagbayad",
        "tanggap ba gcash", "tanggap ba maya", "pwede card diri",
        "paano ang bayad", "ano ang pagbayad", "pila babayaran"
    ],
    "return policy": [
        "return", "refund", "damage", "wrong", "incorrect", "issue",
        "problem", "broken", "complaint", "replace", "replacement",
        "damaged cake", "wrong order", "money back",
        "sira", "mali", "ibalik", "irefund", "pera", "balik pera",
        "hindi tama", "nasira", "may problema", "reklamo", "di ok",
        "ayaw", "palitan", "nasira yung cake", "mali yung order",
        "hindi yan yung inorder ko", "pwede ibalik", "pwede bang irefund",
        "paano mag-reklamo", "may sira", "hindi maganda", "basag",
        "nadurog", "hindi tama order", "paano kung mali",
        "paano kung sira", "gusto mag refund",
        "may problema ang cake", "sira ang cake", "indi tama ang order",
        "gusto ko ibalik", "paano mag-reklamo", "mali ang nahatod",
        "pwede ba ibalik", "gusto ko refund", "nadurog ang cake",
        "indi ko気 gusto", "may depekto", "indi tama",
        "bawion ang bayad", "iuli ang pera", "sira ang nahatod"
    ],
    "pricing": [
        "price", "pricing", "how much", "cost", "quote", "rate",
        "price list", "how much is", "price of cake",
        "magkano", "presyo", "halaga", "pila", "tag-pila", "mahal", "mura",
        "magkano ang cake", "anong presyo", "may price list ba",
        "magkano yung", "gaano kamahal", "mura ba",
        "may listahan ba ng presyo", "price ng cake",
        "how much yung", "anong halaga", "bayad magkano",
        "pila ang cake", "pila ang presyo", "tag-pila", "mahal ba",
        "may price list ba kamo", "pila ang custom cake",
        "anong presyo sang cake", "pila ang bayad", "pila sang cake",
        "mahal ba ang cake", "mura ba diri", "pila gid"
    ],
    "cancellation": [
        "cancel", "cancellation", "withdraw", "back out", "cancel order",
        "cancel my order", "can i cancel", "how to cancel",
        "kanselahin", "bawiin", "hindi na", "ayaw na", "di na tuloy",
        "icancel", "pwede i-cancel", "pwede cancel", "cancel ba",
        "icancel order", "bawiin order", "gusto ko i-cancel",
        "ayaw ko na", "di na matutuloy", "bawiin ko na lang",
        "pwede bang bawiin", "hindi na itutuloy", "i-cancel na lang",
        "mag-cancel ako", "paano mag-cancel", "pwede pa bang i-cancel",
        "gusto ko i-cancel", "pwede pa i-cancel", "indi na ako magorder",
        "bawion ko na lang", "indi na matuloy", "paano mag-cancel",
        "pwede pa bawion", "ayaw ko na", "indi na ko magpadayon",
        "kanselahon ko", "bawion ang order", "indi na tuloy"
    ],
    "order status": [
        "status", "where is my order", "tracking", "update", "my order",
        "order update", "has my order been confirmed", "is my order ready",
        "nasaan", "na deliver na ba", "naihatid na", "order ko",
        "ano na order ko", "kailan dating", "delivered na ba",
        "nasaan na yung order ko", "kelan darating order ko",
        "naka-receive na ba", "update naman", "may update ba",
        "anong status ng order ko", "na-confirm na ba", "na-process na ba",
        "ilang araw pa", "kailan ko matatanggap", "naipadala na ba",
        "napadala na ba", "anong nangyari sa order ko",
        "diin na ang order ko", "nahatod na ba", "san-o mahatod",
        "may update ba ang order ko", "ano na ang order ko",
        "napadala na ba", "kailan ko mabaton", "na-confirm na ba",
        "san-o ko mabaton", "kumusta ang order ko", "ano na status",
        "naprocesso na ba", "ready na ba ang order ko"
    ],
    "minimum order": [
        "minimum", "minimum order", "smallest", "one cake", "small order",
        "least amount", "how many minimum",
        "isang cake", "isa lang", "pwede isang", "maliit na order",
        "pwede isang cake lang", "isa lang ba pwede", "kailangan ba marami",
        "pwede ba kahit isa", "minimum na order", "ilang cake minimum",
        "isa lang pwede", "kailangan ba madamo", "pwede isa ka cake lang",
        "pila ka cake minimum", "pwede gamay nga order",
        "isa lang nga cake", "pwede gamay order", "minimum nga order"
    ],
    "location": [
        "location", "address", "where are you", "where is", "directions",
        "how to get", "shop", "google maps", "how to get there",
        "where is your shop", "store location",
        "saan", "nasaan kayo", "lugar", "saan ang shop",
        "saan kayo naroroon", "nasaan ang tindahan", "paano pumunta",
        "anong address", "san nandito", "san kayo",
        "directions papunta", "saan makikita", "malapit ba sa",
        "san located",
        "diin kamo", "asa kamo", "diin ang tindahan ninyo",
        "paano makabot dira", "ano ang address ninyo",
        "malapit ba sa", "diin located", "asa ang shop",
        "diin kamo sa iloilo", "asa kamo sa iloilo",
        "paano makaabot", "diin ang shop ninyo"
    ],
    "greeting": [
        "hi", "hello", "hey", "good morning", "good afternoon",
        "good evening", "sup", "yo", "good day", "greetings",
        "kumusta", "musta", "helo", "magandang", "magandang umaga",
        "magandang hapon", "magandang gabi", "kamusta", "kamusta kayo",
        "huy", "uy", "ayos ba", "hello po", "hi po",
        "maayong aga", "maayong hapon", "maayong gabi",
        "kamusta gid", "ay helo", "hoy", "kumusta man",
        "maayo man", "kumusta na", "maayong adlaw",
        "who are you", "what are you", "are you a bot", "are you ai",
        "sino ka", "ano ka", "bot ka ba", "ai ka ba",
        "ikaw nga ano", "sin-o ka", "ano ang ngalan mo",
        "whats your name", "what is your name", "your name"
    ],
    "downpayment": [
        "downpayment", "down payment", "deposit", "50%", "advance payment",
        "partial payment", "half payment", "dp", "required deposit",
        "do i need to pay deposit", "how much deposit",
        "bayad muna", "paunang bayad", "magkano dp", "kailangan ba ng dp",
        "bayad agad", "kalahati", "50 porsyento", "may dp ba",
        "paano magbayad ng dp", "kelan magbabayad ng dp",
        "required ba ang dp", "kailangan ng downpayment",
        "magkano ang downpayment", "paano ang downpayment",
        "advance bayad", "bayad antes", "bago gawin", "bayad bago",
        "pila ang dp", "kailangan ba dp", "paano magbayad sang dp",
        "san-o magbayad ng dp", "required ba ang downpayment",
        "pila ang downpayment", "bayad antes magsugod",
        "kailangan ba mag-dp", "pila ang advance payment",
        "paano ang dp", "may dp ba kamo", "bayad anay antes"
    ],
}

# ── GEMINI SETUP 
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
FAQ_CONTEXT = """
    You are a friendly and professional chatbot assistant for Mrs. Brave's Cake Shop in Iloilo City.
    Your name is Brave Bot.

    BEHAVIOR RULES:
    - Only answer questions related to Mrs. Brave's Cake Shop
    - If the question is unrelated, politely redirect to shop topics
    - If the customer is rude or uses bad words, stay calm and professional, do not engage
    - If the customer sends nonsense or gibberish, politely ask them to clarify
    - If you don't know the answer, tell them to contact the owner directly
    - Always reply in the same language the customer used (English, Tagalog, or Hiligaynon)
    - Keep answers short, friendly, and helpful
    - Never make up information not listed below
    - When explaining custom orders, only mention the fields that are explicitly listed
      in the CUSTOMIZATION section above
    - Never talk about other businesses or competitors
    - If asked who you are, say you are Brave Bot, the assistant of Mrs. Brave's Cake Shop
    SHOP INFO:
    - Address: R. Mapa Street, Mandurriao, Iloilo City
    - Contact: 0956 350 4486
    - Hours: Monday to Saturday, 11:00 AM to 10:00 PM
    - Facebook: Mrs. Brave's Cake Shop

    ORDERING:
    - Browse cakes or customize your own
    - Choose size, flavor, design, delivery date
    - We confirm your order and notify you

    DELIVERY:
    - Within Metro Iloilo and nearby areas
    - Delivery fee: P50
    - Choose delivery date and time at checkout

    PICKUP:
    - Free pickup at R. Mapa Street, Mandurriao, Iloilo City
    - Choose pickup date and time at checkout

    CUSTOMIZATION:
    - Customers use the "Customize Your Cake" form in the website.
    - The form asks for: occasion, event date, serving size, flavor, filling, frosting,
      theme or peg, color motif, message on cake, and budget range.
    - Customers can also upload a reference/peg image.
    - After they submit, the owner reviews the details and confirms via chat or notification.
    - 50% downpayment is required before production; orders cannot be cancelled once confirmed.
    PAYMENT:
    - Cash on Delivery (COD)
    - GCash
    - PayMaya
    - Debit or Credit Card
    - QRPH

    DOWNPAYMENT:
    - 50% required for custom cake orders
    - Accepted via GCash, PayMaya, Card, QRPH
    - Non-refundable once production starts

    CANCELLATION:
    - Cannot cancel once confirmed
    - Downpayments are non-refundable
    - If issue is on our end, full refund will be processed

    RETURN POLICY:
    - Refund only if cake arrives damaged or incorrect
    - Contact within 24 hours of delivery
    - Send photos of the issue
    - Full refund or replacement offered

    PRICING:
    - Varies by size, design, flavor
    - Browse website for prices
    - Custom cake quotes available via chat

    MINIMUM ORDER:
    - No minimum, you can order just one cake
    - Bulk orders available, chat owner for special pricing
"""
# HELPERS
# AI FALLBACK FUNCTION
def ai_fallback(user_message):
    try:
        prompt = (
            f"{FAQ_CONTEXT}\n\n"
            "You are Brave Bot. This is a LAST RESORT. "
            "If the question is unclear, unrelated, or too complex, "
            "politely tell the customer to use the quick buttons or Chat with Owner instead of guessing.\n"
            "Keep your answer 1–3 short sentences.\n\n"
            f"Customer: {user_message}\n"
            "Brave Bot:"
        )
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt
        )
        return response.text
    except Exception as e:
        logger.error(f"Gemini fallback failed: {str(e)}")
        return FAQ["default"]

# MAIN FAQ FUNCTION 
def get_faq_response(user_message):
    user_message_lower = " ".join(user_message.lower().split())

    best_match = None
    best_score = 0

    for faq_key, keywords in FAQ_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in user_message_lower)
        if score > best_score:
            best_score = score
            best_match = faq_key

    if best_match and best_score > 0:
        return FAQ[best_match]  # keywords fired, no AI cost
    cake_related_markers = [
        "cake", "kek", "keyk", "order", "mag order", "mag-order",
        "custom", "customize", "customized", "brave", "mrs brave",
        "delivery", "pickup", "pick up", "pick-up", "bayad", "payment",
        "presyo", "price", "pila", "magkano"
    ]
    if any(m in user_message_lower for m in cake_related_markers):
        return FAQ["default"]
    return ai_fallback(user_message)  # only runs if keywords fail

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

def safe_float(value, min_val=None, max_val=None):
    try:
        f = float(value) if value else None
        if f is None:
            return None
        if min_val is not None and f < min_val:
            return None
        if max_val is not None and f > max_val:
            return None
        return f
    except (ValueError, TypeError):
        return None

def handle_loyalty_stamp(users_ref, user_id, order_type, selected_items, cakes_ref, order_id=None):
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

        # Fix 1: double-stamp guard
        if order_id:
            if order_id in user_data.get('stamped_order_ids', []):
                return

        old_stamps        = int(user_data.get('loyalty_stamps', 0))
        loyalty_unclaimed = user_data.get('loyalty_unclaimed', None)
        unclaimed_tier    = user_data.get('loyalty_unclaimed_tier', None)
        new_stamps        = old_stamps + earns_stamps

        update = {}

        # Fix 3: consistent crossing logic for both tiers
        if old_stamps < 10 <= new_stamps:
            update['loyalty_stamps'] = new_stamps
            if not (loyalty_unclaimed and unclaimed_tier == 10):
                update['loyalty_unclaimed']      = True
                update['loyalty_unclaimed_tier'] = 10

        elif old_stamps < 5 <= new_stamps:
            update['loyalty_stamps'] = new_stamps
            if not loyalty_unclaimed:
                update['loyalty_unclaimed']      = True
                update['loyalty_unclaimed_tier'] = 5
        else:
            update['loyalty_stamps'] = new_stamps

        # Fix 1: record order_id after stamping
        if order_id:
            update['stamped_order_ids'] = admin_fs.ArrayUnion([order_id])

        user_ref.update(update)

    except Exception:
        logger.exception("[LOYALTY] Failed to handle loyalty stamp")
        
def send_new_order_fcm(db_ref, order_id, customer_name, order_type, rush=False):
    """Send FCM push notification to all admin tokens for a new order."""

    try:

        admin_tokens_doc = db_ref.collection('fcm_tokens').document('admins').get()
        if not admin_tokens_doc.exists:
            logger.warning('[FCM NEW ORDER] No admin tokens doc found')
            return

        token_map = admin_tokens_doc.to_dict()  # {uid: token}
        if not token_map:
            return

        rush_label = ' 🚨 RUSH' if rush else ''
        type_label = 'Custom Cake' if order_type == 'custom' else 'Premade Cake'

        failed_uids = []
        for uid, token in token_map.items():
            try:
                msg = fcm_messaging.Message(
                    token=token,
                    notification=fcm_messaging.Notification(
                        title=f'🎂 New Order!{rush_label}',
                        body=f'{customer_name} placed a {type_label} order. Tap to review.'
                    ),
                    data={
                        'order_id': order_id,
                        'type': 'new_order',
                        'customer_name': customer_name,
                        'order_type': order_type,
                        'rush': 'true' if rush else 'false'
                    },
                    webpush=fcm_messaging.WebpushConfig(
                        notification=fcm_messaging.WebpushNotification(
                            icon='/static/img/logo.png',
                            badge='/static/img/logo.png',
                            tag='new-order',      # groups them; renotify shows each
                            renotify=True,
                        )
                    )
                )
                fcm_messaging.send(msg)
                logger.info(f'[FCM NEW ORDER] Sent to uid: {uid}')
            except Exception as e:
                logger.warning(f'[FCM NEW ORDER] Failed for uid {uid}: {e}')
                failed_uids.append(uid)

        # Clean up dead tokens
        if failed_uids:
            admin_ref = db_ref.collection('fcm_tokens').document('admins')
            updates = {uid: 'DELETE_FIELD_SENTINEL' for uid in failed_uids}
            # Use firestore.DELETE_FIELD in the caller context

            admin_ref.update({uid: admin_fs.firestore.DELETE_FIELD for uid in failed_uids})

    except Exception as e:
        logger.exception(f'[FCM NEW ORDER] Error: {e}')