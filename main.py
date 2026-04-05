from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from datetime import datetime, timedelta, timezone
from functools import wraps
import re
import os
import json
import cloudinary
import cloudinary.uploader
import firebase
from db import sales, expenses, inventory, users, cakes, walkin_orders, reviews, admin_logs, orders
from firebase_admin import auth, firestore
from pyngrok import ngrok
from paymongo import create_checkout_session, verify_payment, build_line_items
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY')
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024  # 2MB max file size
@app.after_request
def add_ngrok_header(response):
    response.headers['ngrok-skip-browser-warning'] = 'true'
    return response
# ================================================================
# BLUEPRINT REGISTRATION
# ================================================================
from pos import pos_bp
app.register_blueprint(pos_bp)

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
PH_TZ = timezone(timedelta(hours=8))

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

# ================================================================
# DECORATORS
# ================================================================

def login_required(f):
    """Protects customer routes - redirects to login if not logged in"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_id'):
            flash('Please login first!', 'warning')
            return redirect(url_for('auth_page'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    """Protects admin routes - checks admin custom claim"""
    @wraps(f)
    def decorated(*args, **kwargs):
        current_user = session.get('user')
        if not current_user:
            flash('Please login first!', 'warning')
            return redirect(url_for('auth_page'))
        if not current_user.get('admin'):
            return render_template('403.html'), 403
        return f(*args, **kwargs)
    return decorated

# ================================================================
# HELPER FUNCTIONS
# ================================================================
def log_admin_action(action, target, category="general"):
    try:
        admin_logs.add({
            "action": action,
            "target": target,
            "category": category,
            "admin_name": session["user"]["name"],
            "ip_address": request.remote_addr,
            "timestamp": datetime.now(PH_TZ)
        })
    except Exception as e:
        print(f"[LOG ERROR] {e}")

def get_faq_response(user_message):
    """Match user message to FAQ using keyword matching"""
    user_message_lower = user_message.lower()
    for faq_key, faq_answer in FAQ.items():
        if faq_key != "default":
            keywords = faq_key.split()
            if any(keyword in user_message_lower for keyword in keywords):
                return faq_answer
    return FAQ.get("default", "I'm not sure. Please contact us directly!")

def save_uploaded_image(file, upload_type):
    """Upload image to Cloudinary, returns URL or None if invalid/too large"""
    ext = file.filename.rsplit('.', 1)[1].lower()
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
    except Exception as e:
        print(f"Cloudinary upload error: {str(e)}")
        return None

def convert_timestamps(order):
    """Convert Firestore timestamps to PH timezone datetime"""
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

# ================================================================
# PUBLIC ROUTES
# ================================================================

# ---------------- HOME PAGE ----------------
@app.route("/")
def home_page():
    user_id  = session.get("user_id")
    customer = None
    if user_id:
        doc = users.document(user_id).get()
        if doc.exists:
            customer = doc.to_dict()
 
    # Fetch top 5 rated cakes
    available_cakes = []
    for cake_doc in cakes.where("status", "==", True).stream():
        cake_data = cake_doc.to_dict()
        cake_data['id']           = cake_doc.id
        cake_data['avg_rating']   = 0
        cake_data['review_count'] = 0
        available_cakes.append(cake_data)
 
    # Calculate ratings
    cake_ratings = {}
    for r_doc in reviews.where("is_visible", "==", True).stream():
        r   = r_doc.to_dict()
        cid = r.get("cake_id")
        if cid not in cake_ratings:
            cake_ratings[cid] = {"total": 0, "count": 0}
        cake_ratings[cid]["total"] += r.get("rating", 0)
        cake_ratings[cid]["count"] += 1
 
    for cake in available_cakes:
        if cake["id"] in cake_ratings:
            data = cake_ratings[cake["id"]]
            cake["avg_rating"]   = round(data["total"] / data["count"], 1)
            cake["review_count"] = data["count"]
 
    # Sort by rating descending → top 5
    top_cakes = sorted(
        [c for c in available_cakes if c["review_count"] > 0],
        key=lambda x: x["avg_rating"],
        reverse=True
    )[:5]
 
    return render_template("home.html",
        customer=customer,
        top_cakes=top_cakes
    )
@app.route("/privacy-policy")
def privacy_policy():
    return render_template("privacy_policy.html")
# ---------------- AUTHENTICATION ----------------
@app.route("/authentication")
def auth_page():
    return render_template("authentication.html")

@app.route('/forgot-password')
def forgot_password_page():
    return render_template('forgot_password.html')

# ---------------- LOGOUT ----------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth_page"))

# ---------------- VERIFY TOKEN ----------------
@app.route('/verify-token', methods=['POST'])
def verify_token():
    data = request.get_json()
    id_token = data.get('idToken')

    if not id_token:
        return jsonify({'error': 'No token provided'}), 400

    try:
        decoded_token = auth.verify_id_token(id_token)
        uid = decoded_token['uid']
        email = decoded_token.get('email', '')
        is_google = decoded_token.get('firebase', {}).get('sign_in_provider') == 'google.com'

        if not is_google and not decoded_token.get('email_verified', False):
            return jsonify({'error': 'Please verify your email first!', 'needs_verification': True}), 401

        user_doc = users.document(uid).get()
        fname = user_doc.to_dict().get("fname", "") if user_doc.exists else "" #name in admin logs
        is_new_user = not user_doc.exists

        if not user_doc.exists:
            users.document(uid).set({
                'email': email,
                'username': email.split('@')[0] if email else '',
                'fname': '', 'number': '', 'address': '',
                'role': 'customer',
                'created_at': firestore.SERVER_TIMESTAMP
            })

        is_admin = decoded_token.get('admin', False)
        session['user'] = {'uid': uid, 'email': email, 'name': fname or email, 'admin': is_admin}
        session['user_id'] = uid
        session['username'] = email

        return jsonify({'success': True, 'needs_profile': is_new_user, 'is_admin': is_admin}), 200

    except auth.InvalidIdTokenError:
        return jsonify({'error': 'Invalid token'}), 401
    except auth.ExpiredIdTokenError:
        return jsonify({'error': 'Token expired'}), 401
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ---------------- SAVE USER DETAILS ----------------
@app.route('/save-user-details', methods=['POST'])
def save_user_details():
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'Missing or invalid token'}), 401

    id_token = auth_header.split('Bearer ')[1]

    try:
        decoded_token = auth.verify_id_token(id_token)
        token_uid = decoded_token['uid']
        data = request.get_json()
        uid = data.get('uid')

        if uid != token_uid:
            return jsonify({'error': 'UID mismatch'}), 403

        users.document(uid).set({
            'username': data.get('username'),
            'number':   data.get('number'),
            'address':  data.get('address'),
            'fname':    data.get('fname'),
            'email':    decoded_token.get('email', ''),
            'role':     'customer',
            'created_at': firestore.SERVER_TIMESTAMP
        })
        return jsonify({'success': True}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ---------------- COMPLETE PROFILE ----------------
@app.route('/complete-profile', methods=['GET', 'POST'])
def complete_profile():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('auth_page'))

    if request.method == 'POST':
        users.document(user_id).update({
            'fname':    request.form.get('fname'),
            'username': request.form.get('username'),
            'number':   request.form.get('number'),
            'address':  request.form.get('address'),
        })
        return redirect(url_for('customer_dashboard'))

    doc = users.document(user_id).get()
    customer = doc.to_dict()
    return render_template('complete_profile.html', customer=customer)

# ================================================================
# CUSTOMER ROUTES
# ================================================================

# ---------------- CUSTOMER DASHBOARD ----------------
@app.route("/customer_dashboard")
@login_required
def customer_dashboard():
    user_id = session.get("user_id")
 
    doc = users.document(user_id).get()
    if not doc.exists:
        return "User not found", 404
    customer = doc.to_dict()
 
    # ── Orders from top-level collection (already sorted by Firestore) ──
    orders_list = []
    for order_doc in orders.where("user_id", "==", user_id).order_by("created_at", direction="DESCENDING").stream():
        order = order_doc.to_dict()
        order["id"] = order_doc.id
        order["notes"] = order.get("notes", "")
        order["reviewed"] = order.get("reviewed", False)

        if order.get("order_type") == "premade" and order.get("selected_items"):
            total = sum(
                float(i.get("subtotal", float(i.get("price", 0)) * int(i.get("quantity", 1))))
                for i in order["selected_items"]
            )
            order["calculated_total"] = total
        else:
            order["calculated_total"] = order.get("amount", 0)

        order = convert_timestamps(order)
        orders_list.append(order)
        
    # Favorites
    favorite_ids = []
    favorites_list = []
 
    for fav_doc in users.document(user_id).collection("favorites").stream():
        cake_id = fav_doc.id
        favorite_ids.append(cake_id)
        cake_doc = cakes.document(cake_id).get()
        if cake_doc.exists:
            cake_data = cake_doc.to_dict()
            cake_data['id'] = cake_doc.id
            cake_data['avg_rating'] = 0
            cake_data['review_count'] = 0
            favorites_list.append(cake_data)
 
    if favorites_list:
        fav_id_set = {c['id'] for c in favorites_list}
        cake_ratings = {}
        for r_doc in reviews.where("is_visible", "==", True).stream():
            r = r_doc.to_dict()
            cid = r.get("cake_id")
            if cid in fav_id_set:
                if cid not in cake_ratings:
                    cake_ratings[cid] = {"total": 0, "count": 0}
                cake_ratings[cid]["total"] += r.get("rating", 0)
                cake_ratings[cid]["count"] += 1
        for cake in favorites_list:
            if cake["id"] in cake_ratings:
                data = cake_ratings[cake["id"]]
                cake["avg_rating"] = round(data["total"] / data["count"], 1)
                cake["review_count"] = data["count"]
 
    cart_count = len(list(users.document(user_id).collection("cart").stream()))
 
    return render_template("customer_dashboard.html",
        customer=customer,
        orders=orders_list,
        user_id=user_id,
        cart_count=cart_count,
        favorite_ids=favorite_ids,
        favorites_list=favorites_list,
    )

# ---------------- FAVORITES TOGGLE ----------------
@app.route("/favorites/toggle", methods=["POST"])
@login_required
def favorites_toggle():
    try:
        user_id   = session.get("user_id")
        data      = request.get_json()
        cake_id   = data.get("cake_id")
        cake_name = data.get("cake_name", "")
 
        if not cake_id:
            return jsonify({"success": False, "error": "Missing cake_id"}), 400
 
        fav_ref = users.document(user_id).collection("favorites").document(cake_id)
        fav_doc = fav_ref.get()
 
        if fav_doc.exists:
            fav_ref.delete()
            return jsonify({"success": True, "action": "removed"})
        else:
            fav_ref.set({
                "cake_id":   cake_id,
                "cake_name": cake_name,
                "added_at":  datetime.now(PH_TZ)
            })
            return jsonify({"success": True, "action": "added"})
 
    except Exception as e:
        print(f"[FAVORITES ERROR] {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# ---------------- CUSTOMER PROFILE EDIT ----------------
@app.route("/customer/edit", methods=["POST"])
@login_required
def edit_customer_profile():
    user_id = session.get("user_id")
    users.document(user_id).update({
        "username": request.form.get("username"),
        "number":   request.form.get("contact"),
        "address":  request.form.get("address"),
        "fname":    request.form.get("full_name")
    })
    return redirect(url_for("customer_dashboard"))

# ---------------- CUSTOMIZE CAKE PAGE ----------------
@app.route('/customize_cake')
def customize():
    user_id = session.get('user_id')
    customer = None
    if user_id:
        doc = users.document(user_id).get()
        if doc.exists:
            customer = doc.to_dict()
    return render_template('customization.html', customer=customer)

# ---------------- ADD REVIEW PAGE ----------------
@app.route("/review/add", methods=["POST"])
@login_required
def add_review():
    user_id  = session.get("user_id")
    now      = datetime.now(PH_TZ)
    
    cake_id       = request.form.get("cake_id")
    cake_name     = request.form.get("cake_name")
    order_id      = request.form.get("order_id")
    comment       = request.form.get("comment", "").strip()
    
    # Get reviewer name from Firestore
    user_doc      = users.document(user_id).get()
    reviewer_name = "Customer"
    if user_doc.exists:
        reviewer_name = user_doc.to_dict().get("fname") or \
                        user_doc.to_dict().get("username") or \
                        "Customer"
    
    # Check if already reviewed this order
    order_ref = users.document(user_id).collection("orders").document(order_id)
    order_doc = order_ref.get()
    if not order_doc.exists:
        flash("Order not found.", "danger")
        return redirect(url_for("customer_dashboard"))
    
    order_data = order_doc.to_dict()
    if order_data.get("reviewed"):
        flash("You already reviewed this order.", "warning")
        return redirect(url_for("customer_dashboard"))
    
    # Build review data based on order type
    review_data = {
        "user_id":       user_id,
        "order_id":      order_id,
        "cake_id":       cake_id,
        "cake_name":     cake_name,
        "comment":       comment,
        "reviewer_name": reviewer_name,
        "is_visible":    True,
        "created_at":    now,
        "order_type":    order_data.get("order_type")
    }
    
    # For custom orders, include flavor, design, overall ratings
    if order_data.get("order_type") == "custom":
        flavor_rating = int(request.form.get("flavor_rating", 5))
        design_rating = int(request.form.get("design_rating", 5))
        overall_rating = int(request.form.get("rating", 5))
        
        review_data["flavor_rating"] = flavor_rating
        review_data["design_rating"] = design_rating
        review_data["overall_rating"] = overall_rating
        review_data["rating"] = overall_rating  # For compatibility
        
    else:
        # Premade orders: single rating
        rating = int(request.form.get("rating", 5))
        review_data["rating"] = rating
    
    # Save review
    reviews.add(review_data)
    
    # Mark order as reviewed
    order_ref.update({"reviewed": True})
    
    flash("Review submitted! Thank you 🎂", "success")
    return redirect(url_for("customer_dashboard"))
#RECEIPT
@app.route("/order/receipt/<order_id>")
@login_required
def order_receipt(order_id):
    user_id = session.get("user_id")
    
    # Get order from top-level collection
    order_ref = orders.document(order_id)  # ← CHANGED
    order_doc = order_ref.get()
    
    if not order_doc.exists:
        flash("Receipt not found.", "danger")
        return redirect(url_for("customer_dashboard"))
    
    order = order_doc.to_dict()
    
    # Verify order belongs to current user
    if order.get("user_id") != user_id:
        flash("Unauthorized access.", "danger")
        return redirect(url_for("customer_dashboard"))
    
    order["id"] = order_id
    order = convert_timestamps(order)
    
    if order.get("status") != "Completed" and order.get("payment_status") != "Paid":
        flash("Receipt is only available for completed or paid orders.", "warning")
        return redirect(url_for("customer_dashboard"))
    
    if order.get("order_type") == "premade" and order.get("selected_items"):
        total = 0
        for item in order["selected_items"]:
            subtotal = item.get("subtotal", item.get("price", 0) * item.get("quantity", 1))
            total += float(subtotal)
        order["calculated_total"] = total
    else:
        order["calculated_total"] = order.get("amount", 0)
    
    return render_template("customer_receipt.html", order=order)

# ================================================================
# ORDER ROUTES
# ================================================================

# ---------------- CUSTOMIZATION ORDER ----------------
@app.route("/order", methods=["POST"])
@login_required
def place_order():
    user_id = session.get("user_id")

    file = request.files.get('image')
    if file and file.filename:
        inspo_image = save_uploaded_image(file, 'order')
        if inspo_image is None:
            flash('Image too large or invalid! Max 2MB.', 'danger')
            return redirect(url_for('customize'))
    else:
        inspo_image = None

    customer_doc = users.document(user_id).get()
    customer = customer_doc.to_dict() if customer_doc.exists else {}
    min_date = (datetime.now(PH_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")

    return render_template('checkout.html',
        order_type     = 'custom',
        order_item     = request.form.get('order_item'),
        amount         = request.form.get('amount'),
        notes          = request.form.get('notes', ''),
        rush           = request.form.get('rush', ''),
        inspo_image    = inspo_image,
        selected_items = [],
        customer       = customer,
        min_date       = min_date,
    )

# ---------------- PREMADE ORDER ----------------
@app.route("/order/cake", methods=["POST"])
@login_required
def order_cake():
    user_id = session.get("user_id")
 
    customer_doc = users.document(user_id).get()
    customer     = customer_doc.to_dict() if customer_doc.exists else {}
    min_date     = (datetime.now(PH_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
 
    selected_json  = request.form.get('selected_items', '[]')
    selected_items = json.loads(selected_json)
 
    if selected_items:
        # coming from cart — already has quantity + subtotal
        for i in selected_items:
            i['quantity'] = int(i.get('quantity', 1))
            i['subtotal'] = float(i['price']) * i['quantity']
            
            # Get image URL from cakes collection if not present
            if 'image_url' not in i and i.get('cake_id'):
                cake_doc = cakes.document(i['cake_id']).get()
                if cake_doc.exists:
                    cake_data = cake_doc.to_dict()
                    i['image_url'] = cake_data.get('image', None)
            
        amount = sum(float(i['subtotal']) for i in selected_items)
    else:
        # coming from Order Now on cakes page
        quantity = int(request.form.get('quantity', 1))
        price    = float(request.form.get('price', 0))
        cake_id  = request.form.get('cake_id')
        cake_name = request.form.get('cake_name')
        
        # Get cake details including image from database
        cake_doc = cakes.document(cake_id).get()
        cake_data = cake_doc.to_dict() if cake_doc.exists else {}
        
        selected_items = [{
            'cake_id':   cake_id,
            'cake_name': cake_name,
            'price':     price,
            'quantity':  quantity,
            'subtotal':  price * quantity,
            'image_url': cake_data.get('image', None)  # This is the Cloudinary URL
        }]
        amount = price * quantity
 
    return render_template('checkout.html',
        order_type     = 'premade',
        selected_items = selected_items,
        amount         = amount,
        customer       = customer,
        min_date       = min_date,
    )
 

# ---------------- PLACE ORDER (SAVE TO FIRESTORE) ----------------
@app.route("/place-order", methods=["POST"])
@login_required
def finalize_order():
    user_id = session.get("user_id")
    now     = datetime.now(PH_TZ)
 
    date_str          = request.form["delivery_date"]
    time_str          = request.form["delivery_time"]
    delivery_datetime = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    delivery_datetime = delivery_datetime.replace(tzinfo=PH_TZ)
 
    delivery_type  = request.form.get("delivery_type", "Delivery")
    address        = "Pick Up at Shop" if delivery_type == "Pickup" else request.form.get("address", "")
    order_type     = request.form.get("order_type")
    selected_json  = request.form.get("selected_items", "[]")
    payment_method = request.form.get("payment_method", "Cash on Delivery")
 
    if order_type == "premade":
        selected_items = json.loads(selected_json)
        item_names     = ", ".join([f"{i['cake_name']} (₱{float(i['price']):.0f})" for i in selected_items])
        amount         = sum(float(i["price"]) for i in selected_items)
        rush           = False
        inspo_image    = None
        for i in selected_items:
            users.document(user_id).collection("cart").document(i["cake_id"]).delete()
        custom_components = []
    else:
        selected_items = []
        item_names     = request.form.get("order_item", "")
        amount         = float(request.form.get("amount", 0))
        rush           = request.form.get("rush") == "yes"
        inspo_image    = request.form.get("inspo_image") or None
        
        custom_components = []
        item_parts = item_names.split(", ")
        
        for part in item_parts:
            match = re.search(r'(.+) \(₱([\d,]+)\)', part)
            if match:
                component_name = match.group(1).strip()
                component_price = float(match.group(2).replace(',', ''))
                custom_components.append({
                    "name": component_name,
                    "price": component_price
                })
            else:
                match2 = re.search(r'(.+) \(([\d,]+)\)', part)
                if match2:
                    component_name = match2.group(1).strip()
                    component_price = float(match2.group(2).replace(',', ''))
                    custom_components.append({
                        "name": component_name,
                        "price": component_price
                    })
 
    # ── Base order data with user_id ──
    order_data = {
        "user_id":        user_id,  # ← ADDED: link to user
        "delivery_date":  delivery_datetime,
        "item":           item_names,
        "selected_items": selected_items,
        "amount":         amount,
        "status":         "New",
        "rush":           rush,
        "notes":          request.form.get("notes", ""),
        "payment_method": payment_method,
        "payment_status": "Pending",
        "payment_id":     None,
        "delivery_type":  delivery_type,
        "inspo_image":    inspo_image,
        "order_type":     order_type,
        "custom_components": custom_components,
        "customer": {
            "name":      request.form.get("customer_name", ""),
            "contact":   request.form.get("contact", ""),
            "address":   address,
            "occasion":  request.form.get("occasion", ""),
            "celebrant": request.form.get("celebrant", ""),
            "age":       request.form.get("age", "")
        },
        "created_at": now
    }
 
    # Deduct cake quantity for premade orders
    if order_type == "premade":
        for item in selected_items:
            cake_id = item.get("cake_id")
            quantity_ordered = item.get("quantity", 1)
            cake_ref = cakes.document(cake_id)
            cake_doc = cake_ref.get()
            if cake_doc.exists:
                current_qty = cake_doc.to_dict().get("quantity", 0)
                new_qty = current_qty - quantity_ordered
                cake_ref.update({"quantity": new_qty})
 
    # ── COD or Bank Transfer → save immediately to top-level orders ──
    if payment_method in ["Cash on Delivery", "Bank Transfer"]:
        orders.add(order_data)  # ← CHANGED: save to top-level orders collection
        flash("Order placed successfully! 🎂", "success")
        return redirect(url_for("customer_dashboard"))
 
    # ── GCash / Maya / Card → PayMongo checkout ──
    line_items = build_line_items(order_type, selected_items, item_names, amount)
 
    session['pending_order'] = {
        "user_id":    user_id,
        "order_data": {
            **order_data,
            "delivery_date": delivery_datetime.isoformat(),
            "created_at":    now.isoformat()
        }
    }
 
    base_url    = request.host_url.rstrip('/')
    success_url = f"{base_url}/payment/success"
    cancel_url  = f"{base_url}/payment/failed"
 
    checkout = create_checkout_session(
        amount            = int(amount * 100),
        order_description = f"Ms. Brave Cake Shop - {item_names[:100]}",
        line_items        = line_items,
        success_url       = success_url,
        cancel_url        = cancel_url
    )
 
    if not checkout:
        flash("Payment service unavailable. Please try Cash on Delivery.", "danger")
        return redirect(url_for("customer_dashboard"))
 
    session['paymongo_session_id'] = checkout["session_id"]
    return redirect(checkout["checkout_url"])
# ---------------- CUSTOMER CANCEL ORDER ----------------
@app.route("/order/cancel/<order_id>", methods=["POST"])
@login_required
def cancel_order(order_id):
    user_id = session.get("user_id")

    order_ref = orders.document(order_id)  # ← CHANGED
    order_doc = order_ref.get()

    if not order_doc.exists:
        flash("Order not found.", "danger")
        return redirect(url_for("customer_dashboard"))
    
    order = order_doc.to_dict()
    
    # Verify order belongs to current user
    if order.get("user_id") != user_id:
        flash("Unauthorized access.", "danger")
        return redirect(url_for("customer_dashboard"))

    if order.get("status") != "New":
        flash("Order cannot be cancelled anymore.", "warning")
        return redirect(url_for("customer_dashboard"))

    order_ref.update({"status": "Cancelled"})
    flash("Order cancelled successfully.", "info")
    return redirect(url_for("customer_dashboard"))

# ================================================================
# CAKES ROUTES
# ================================================================
@app.route("/cleanup-old-orders")
@admin_required
def cleanup_old_orders():
    count = 0
    for user_doc in users.stream():
        user_id = user_doc.id
        # Get all orders from subcollection
        for order_doc in users.document(user_id).collection("orders").stream():
            order_doc.reference.delete()
            count += 1
            print(f"Deleted old order {order_doc.id} from user {user_id}")
    
    return f"Cleaned up {count} old orders from subcollections"
# ---------------- AVAILABLE CAKES PAGE ----------------
@app.route("/cakes")
def cakes_page():
    available_cakes = []
    for cake_doc in cakes.where("status", "==", True).stream():
        cake_data = cake_doc.to_dict()
        cake_data['id'] = cake_doc.id
        cake_data['avg_rating'] = 0
        cake_data['review_count'] = 0
        cake_data['reviews'] = []
        available_cakes.append(cake_data)

    # Fetch reviews with Firestore ordering (no lambda)
    cake_ratings = {}
    cake_reviews = {}

    for r_doc in reviews.where("is_visible", "==", True).order_by("created_at", direction="DESCENDING").stream():
        r = r_doc.to_dict()
        cid = r.get("cake_id")

        # Store ratings
        if cid not in cake_ratings:
            cake_ratings[cid] = {"total": 0, "count": 0}
        cake_ratings[cid]["total"] += r.get("rating", 0)
        cake_ratings[cid]["count"] += 1

        # Store full review
        if cid not in cake_reviews:
            cake_reviews[cid] = []

        created_at = r.get("created_at")
        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
            else:
                created_at = created_at.astimezone(PH_TZ)

        cake_reviews[cid].append({
            "rating": r.get("rating", 5),
            "comment": r.get("comment", ""),
            "reviewer_name": r.get("reviewer_name", "Customer"),
            "created_at": created_at
        })

    # Attach to cakes
    for cake in available_cakes:
        cid = cake["id"]
        if cid in cake_ratings:
            data = cake_ratings[cid]
            cake["avg_rating"] = round(data["total"] / data["count"], 1)
            cake["review_count"] = data["count"]
        
        # Already sorted from Firestore, no lambda needed
        cake["reviews"] = cake_reviews.get(cid, [])

    user_id = session.get("user_id")
    favorite_ids = []
    if user_id:
        for doc in users.document(user_id).collection("favorites").stream():
            favorite_ids.append(doc.id)

    return render_template("cakes.html",
        cakes=available_cakes,
        user_id=user_id,
        favorite_ids=favorite_ids
    )

# ================================================================
# CART ROUTES
# ================================================================

# ---------------- CART PAGE ----------------
@app.route("/cart")
@login_required
def cart_page():
    user_id = session.get("user_id")
    cart_items = []
    for doc in users.document(user_id).collection("cart").stream():
        item = doc.to_dict()
        item["id"] = doc.id
        cart_items.append(item)
    return render_template("cart.html", cart_items=cart_items)

# ---------------- ADD TO CART ----------------
@app.route("/cart/add", methods=["POST"])
@login_required
def add_to_cart():
    user_id   = session.get("user_id")
    cake_id   = request.form.get("cake_id")
    cake_name = request.form.get("cake_name")
    price     = float(request.form.get("price", 0))
    quantity  = int(request.form.get("quantity", 1))
 
    cart_ref = users.document(user_id).collection("cart").document(cake_id)
    cart_doc = cart_ref.get()
 
    if cart_doc.exists:
        # add to existing quantity
        existing_qty = cart_doc.to_dict().get("quantity", 1)
        new_qty      = existing_qty + quantity
        cart_ref.update({"quantity": new_qty})
        flash(f"{cake_name} quantity updated to {new_qty} in cart! 🛒", "success")
    else:
        cart_ref.set({
            "cake_id":   cake_id,
            "cake_name": cake_name,
            "price":     price,
            "quantity":  quantity,
            "added_at":  firestore.SERVER_TIMESTAMP
        })
        flash(f"{cake_name} added to cart! 🛒", "success")
 
    return redirect(url_for("cakes_page"))

# ---------------- REMOVE FROM CART ----------------
@app.route("/cart/remove/<cake_id>", methods=["POST"])
@login_required
def remove_from_cart(cake_id):
    user_id = session.get("user_id")
    users.document(user_id).collection("cart").document(cake_id).delete()
    flash("Item removed from cart.", "info")
    return redirect(url_for("cart_page"))

# ================================================================
# ADMIN ROUTES
# ================================================================

# ---------------- ADMIN DASHBOARD ----------------
@app.route("/admin/dashboard")
@admin_required
def admin_page():
    low_stock = [doc.to_dict() for doc in inventory.where("quantity", "<", 10).stream()]

    orders = []
    for user_doc in users.stream():
        for order_doc in users.document(user_doc.id).collection("orders").stream():
            order = order_doc.to_dict()
            order["id"] = order_doc.id
            order = convert_timestamps(order)
            orders.append(order)

    total_new = total_accepted = total_pending = total_ready = 0
    total_out = total_completed = total_cancelled = total_rush = 0
    today_count = 0
    today_deliveries = []
    today_date = datetime.now(PH_TZ).date()

    for order in orders:
        status = order.get("status", "")
        if status == "New":                total_new += 1
        elif status == "Accepted":         total_accepted += 1
        elif status == "Pending":          total_pending += 1
        elif status == "Ready":            total_ready += 1
        elif status == "Out for Delivery": total_out += 1
        elif status == "Completed":        total_completed += 1
        elif status == "Cancelled":        total_cancelled += 1
        if order.get("rush"):              total_rush += 1

        delivery_date = order.get("delivery_date")
        if isinstance(delivery_date, datetime) and delivery_date.date() == today_date:
            if status not in ["Completed", "Cancelled"]:
                today_count += 1
                today_deliveries.append({
                    "time":     delivery_date.strftime("%I:%M %p"),
                    "customer": order.get("customer", {}).get("name", "N/A"),
                    "cake":     order.get("item", "N/A"),
                    "status":   status,
                    "rush":     order.get("rush", False)
                })

    today_deliveries.sort(key=lambda x: datetime.strptime(x["time"], "%I:%M %p"))

    return render_template("admin_dashboard.html",
        low_stock=low_stock,
        total_new=total_new, total_accepted=total_accepted,
        total_pending=total_pending, total_ready=total_ready,
        total_out=total_out, total_completed=total_completed,
        total_cancelled=total_cancelled, total_rush=total_rush,
        today_count=today_count, today_deliveries=today_deliveries
    )

# ---------------- ADMIN ORDERS ----------------
@app.route("/admin/orders")
@admin_required
def admin_orders():
    orders_list = []
    
    # ONE simple query instead of looping all users
    for order_doc in orders.order_by("created_at", direction="DESCENDING").stream():  # ← CHANGED
        order = order_doc.to_dict()
        order["id"] = order_doc.id
        
        # Get user info for display
        user_id = order.get("user_id")
        if user_id:
            user_doc = users.document(user_id).get()
            if user_doc.exists:
                user_data = user_doc.to_dict()
                order["customer_username"] = user_data.get("username", "")
                if "customer" not in order:
                    order["customer"] = {
                        "name": user_data.get("fname", ""),
                        "contact": user_data.get("number", ""),
                        "address": user_data.get("address", ""),
                    }
            else:
                order["customer_username"] = "Unknown"
        else:
            order["customer_username"] = "Unknown"
        
        order["notes"] = order.get("notes", "")
        order["inspo_image"] = order.get("inspo_image", None)
        order["order_source"] = order.get("order_source", "online")
        
        if order.get("order_type") == "premade" and order.get("selected_items"):
            total = 0
            for item in order["selected_items"]:
                subtotal = item.get("subtotal", item.get("price", 0) * item.get("quantity", 1))
                total += float(subtotal)
            order["calculated_total"] = total
        else:
            order["calculated_total"] = order.get("amount", 0)
        
        order = convert_timestamps(order)
        orders_list.append(order)
    
    # Fetch walk-in orders (keep as is)
    for order_doc in walkin_orders.stream():
        order = order_doc.to_dict()
        order["id"] = order_doc.id
        order["user_id"] = None
        order["order_source"] = "walk-in"
        order["notes"] = order.get("notes", "")
        order["inspo_image"] = None
        order["rush"] = False
        order["delivery_type"] = "Walk-in"
        order["payment_method"] = order.get("payment_method", "Cash")
        order["calculated_total"] = order.get("amount", 0)
        order["delivery_date"] = order.get("created_at") or datetime.now(PH_TZ)
        order["customer"] = {
            "name": "Walk-in Customer",
            "contact": "N/A",
            "address": "N/A",
            "occasion": "N/A",
            "celebrant": "N/A",
            "age": "N/A"
        }
        created_at = order.get("created_at")
        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
            else:
                created_at = created_at.astimezone(PH_TZ)
        order["created_at"] = created_at
        order["delivery_date"] = created_at
        orders_list.append(order)
    
    orders_list.sort(key=lambda x: x["created_at"] or datetime.min.replace(tzinfo=PH_TZ), reverse=True)
    return render_template("admin_orders.html", orders=orders_list)
# ---------------- ADMIN INVENTORY ----------------
@app.route("/admin/inventory")
@admin_required
def admin_inventory():
    inv_items = []
    for doc in inventory.stream():
        item = doc.to_dict()
        item["id"] = doc.id
        inv_items.append(item)
    return render_template("admin_inventory.html", inventory=inv_items)

# ---------------- ADMIN EXPENSES ----------------
@app.route("/admin/expenses")
@admin_required
def admin_expenses():
    exp_items = []
    
    for doc in expenses.order_by("date", direction="DESCENDING").stream():
        e = doc.to_dict()
        e["id"] = doc.id
        date_val = e.get("date")
        if isinstance(date_val, str):
            date_val = datetime.fromisoformat(date_val)
        if isinstance(date_val, datetime):
            if date_val.tzinfo is None:
                date_val = date_val.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
            else:
                date_val = date_val.astimezone(PH_TZ)
        e["date"] = date_val
        exp_items.append(e)
    
    return render_template("admin_expenses.html", expenses=exp_items)
# ---------------- ADMIN SALES ----------------
@app.route("/admin/sales")
@admin_required
def admin_sales():
    sales_items = []
    
    # One query to top-level orders collection
    for order_doc in orders.where("status", "==", "Completed").order_by("created_at", direction="DESCENDING").stream():
    # Error with link → create index → works
        order = order_doc.to_dict()
        order["id"] = order_doc.id
        
        # Get username from users collection
        user_id = order.get("user_id")
        if user_id:
            user_doc = users.document(user_id).get()
            if user_doc.exists:
                order["customer_username"] = user_doc.to_dict().get("username", "")
            else:
                order["customer_username"] = "Unknown"
        else:
            order["customer_username"] = "Unknown"
        
        order = convert_timestamps(order)
        sales_items.append(order)
    
    sales_items.sort(key=lambda x: x["created_at"] or datetime.min.replace(tzinfo=PH_TZ), reverse=True)
    return render_template("admin_sales.html", sales=sales_items)

# ---------------- ADMIN ANALYTICS ----------------
@app.route("/admin/analytics")
@admin_required
def admin_analytics():
    now      = datetime.now(PH_TZ)
    week_ago = now - timedelta(days=7)
    days_order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    months     = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
 
    # ── Weekly ──
    weekly_sales    = {day: 0 for day in days_order}
    weekly_expenses = {day: 0 for day in days_order}
    weekly_profit   = {day: 0 for day in days_order}
 
    # ── Monthly (this year) ──
    monthly_sales    = {m: 0 for m in months}
    monthly_expenses = {m: 0 for m in months}
    monthly_profit   = {m: 0 for m in months}
 
    # ── All time (by year-month) ──
    alltime_data = {}  # "Jan 2025" → {sales, expenses, profit}
 
    # ── Summary cards ──
    total_revenue   = 0
    total_orders    = 0
    total_completed = 0
    payment_counts  = {}
    cake_sales      = {}
 
    # ── Fetch expenses ──
    for doc in expenses.stream():
        e = doc.to_dict()
        date_val = e.get("date")
        if isinstance(date_val, str):
            date_val = datetime.fromisoformat(date_val)
        if isinstance(date_val, datetime):
            if date_val.tzinfo is None:
                date_val = date_val.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
            else:
                date_val = date_val.astimezone(PH_TZ)
 
        cost = float(e.get("cost", 0))
 
        if isinstance(date_val, datetime):
            # Weekly
            if date_val >= week_ago:
                weekly_expenses[date_val.strftime("%a")] += cost
 
            # Monthly (this year)
            if date_val.year == now.year:
                monthly_expenses[date_val.strftime("%b")] += cost
 
            # All time
            key = date_val.strftime("%b %Y")
            if key not in alltime_data:
                alltime_data[key] = {"sales": 0, "expenses": 0, "profit": 0, "sort": date_val}
            alltime_data[key]["expenses"] += cost
 
    # ── Fetch orders ──
    for order_doc in orders.stream():  # ← CHANGED
        order = order_doc.to_dict()
        status = order.get("status", "")
        amount = float(order.get("amount", 0))
        
        total_orders += 1
        if status == "Completed":
            total_completed += 1
        
        payment = order.get("payment_method", "Unknown")
        payment_counts[payment] = payment_counts.get(payment, 0) + 1
        
        if order.get("order_type") == "premade":
            for item in order.get("selected_items", []):
                name = item.get("cake_name", "Unknown")
                cake_sales[name] = cake_sales.get(name, 0) + 1
        
        created_at = order.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
            else:
                created_at = created_at.astimezone(PH_TZ)
        
        if status in ["Completed", "Pickup"]:
            total_revenue += amount
            
            if isinstance(created_at, datetime):
                if created_at >= week_ago:
                    weekly_sales[created_at.strftime("%a")] += amount
                if created_at.year == now.year:
                    monthly_sales[created_at.strftime("%b")] += amount
                key = created_at.strftime("%b %Y")
                if key not in alltime_data:
                    alltime_data[key] = {"sales": 0, "expenses": 0, "profit": 0, "sort": created_at}
                alltime_data[key]["sales"] += amount
    
    # Weekly profit
    for day in days_order:
        weekly_profit[day] = weekly_sales[day] - weekly_expenses[day]
 
    # Monthly profit
    for m in months:
        monthly_profit[m] = monthly_sales[m] - monthly_expenses[m]
 
    # All time profit + sort chronologically
    for key in alltime_data:
        alltime_data[key]["profit"] = alltime_data[key]["sales"] - alltime_data[key]["expenses"]
 
    alltime_sorted = sorted(alltime_data.items(), key=lambda x: x[1]["sort"])
    alltime_labels      = [k for k, v in alltime_sorted]
    alltime_sales_vals  = [v["sales"] for k, v in alltime_sorted]
    alltime_expense_vals= [v["expenses"] for k, v in alltime_sorted]
    alltime_profit_vals = [v["profit"] for k, v in alltime_sorted]
 
    # Top 3 cakes
    top_cakes       = sorted(cake_sales.items(), key=lambda x: x[1], reverse=True)[:3]
    top_cake_names  = [c[0] for c in top_cakes]
    top_cake_counts = [c[1] for c in top_cakes]
 
    # Stats
    completion_rate = round((total_completed / total_orders * 100), 1) if total_orders > 0 else 0
    avg_order_value = round(total_revenue / total_completed, 2) if total_completed > 0 else 0
 
    return render_template("admin_analytics.html",
        now             = now,
        # Weekly
        weekly_sales    = weekly_sales,
        weekly_expenses = weekly_expenses,
        weekly_profit   = weekly_profit,
        days_order      = days_order,
        # Monthly
        monthly_sales   = monthly_sales,
        monthly_expenses= monthly_expenses,
        monthly_profit  = monthly_profit,
        months          = months,
        # All time
        alltime_labels      = alltime_labels,
        alltime_sales_vals  = alltime_sales_vals,
        alltime_expense_vals= alltime_expense_vals,
        alltime_profit_vals = alltime_profit_vals,
        # Summary cards
        total_revenue   = total_revenue,
        total_orders    = total_orders,
        completion_rate = completion_rate,
        avg_order_value = avg_order_value,
        # Charts
        top_cake_names  = top_cake_names,
        top_cake_counts = top_cake_counts,
        payment_counts  = payment_counts,
    )
# ---------------- ADMIN CAKES ----------------
@app.route("/admin/cakes")
@admin_required
def admin_cakes():
    cakes_list = []
    for cake in cakes.stream():
        cake_data = cake.to_dict()
        cake_data['id'] = cake.id
        cakes_list.append(cake_data)
    return render_template("admin_cakes.html", cakes=cakes_list)

# ---------------- ADMIN USERS ----------------
@app.route("/admin/users")
@admin_required
def admin_users():
    all_users = []
    
    # Get all order counts in ONE query
    order_counts = {}
    for order_doc in orders.stream():  # ← CHANGED
        uid = order_doc.to_dict().get("user_id")
        if uid:
            order_counts[uid] = order_counts.get(uid, 0) + 1
    
    users_ref = users.order_by("created_at", direction="DESCENDING").stream()
    for user_doc in users_ref:
        user_data = user_doc.to_dict()
        user_data['uid'] = user_doc.id
        user_data['order_count'] = order_counts.get(user_doc.id, 0)  # ← CHANGED: no extra query
        
        try:
            auth_user = auth.get_user(user_doc.id)
            user_data['disabled'] = auth_user.disabled
            user_data['email_verified'] = auth_user.email_verified
            user_data['created_at'] = datetime.fromtimestamp(auth_user.user_metadata.creation_timestamp / 1000, tz=PH_TZ)
        except:
            user_data['disabled'] = False
            user_data['email_verified'] = False
            user_data['created_at'] = None
        all_users.append(user_data)
    
    return render_template("admin_users.html", all_users=all_users)

# ADMIN - REVIEWS PAGE
@app.route("/admin/reviews")
@admin_required
def admin_reviews():
    all_reviews = []
    for doc in reviews.order_by("created_at", direction="DESCENDING").stream():
        r = doc.to_dict()
        r["id"] = doc.id
 
        created_at = r.get("created_at")
        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
            else:
                created_at = created_at.astimezone(PH_TZ)
        r["created_at"] = created_at
 
        all_reviews.append(r)
 
    return render_template("admin_reviews.html", reviews=all_reviews)

@app.route("/admin/logs")
@admin_required
def admin_logs_page():
    logs_ref = (
        admin_logs.order_by("timestamp", direction="DESCENDING").limit(200)
    )
    logs = []
    for doc in logs_ref.stream():
        log = doc.to_dict()
        log["id"] = doc.id
        # convert timestamp to PH timezone string
        if log.get("timestamp"):
            ts = log["timestamp"]
            if hasattr(ts, "tzinfo") and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            log["timestamp_str"] = ts.astimezone(PH_TZ).strftime("%b %d, %Y %I:%M %p")
        logs.append(log)
    
    return render_template("admin_logs.html", logs=logs)

# ================================================================
# ADMIN ACTION ROUTES
# ================================================================

# ---------------- UPDATE ORDER STATUS ----------------
@app.route("/order/status/<user_id>/<order_id>", methods=["POST"])
@admin_required
def update_order_status(user_id, order_id):
    new_status = request.form["status"]
    order_ref = orders.document(order_id)  # ← CHANGED
    order_doc = order_ref.get()

    if order_doc.exists:
        order_data = order_doc.to_dict()
        old_status = order_data.get("status")
        order_type = order_data.get("order_type", "custom")

        if new_status == "Accepted" and old_status == "New" and order_type == "premade":
            for i in order_data.get("selected_items", []):
                cake_ref = cakes.document(i["cake_id"])
                cake_doc = cake_ref.get()
                if cake_doc.exists:
                    current_qty = cake_doc.to_dict().get("quantity", 0)
                    new_qty = max(0, current_qty - 1)
                    cake_ref.update({"quantity": new_qty, "status": new_qty > 0})

        accepted_statuses = ["Accepted", "Pending", "Ready", "Out for Delivery"]
        if new_status == "Cancelled" and old_status in accepted_statuses and order_type == "premade":
            for i in order_data.get("selected_items", []):
                cake_ref = cakes.document(i["cake_id"])
                cake_doc = cake_ref.get()
                if cake_doc.exists:
                    current_qty = cake_doc.to_dict().get("quantity", 0)
                    cake_ref.update({"quantity": current_qty + 1, "status": True})

    order_ref.update({"status": new_status})
    log_admin_action(
        action=f"Changed order status to '{new_status}'",
        target=f"Order #{order_id}",
        category="order"
    )
    return redirect(url_for("admin_orders"))

# ---------------- EDIT ORDER ----------------
@app.route("/order/edit/<user_id>/<order_id>", methods=["POST"])
@admin_required
def edit_order(user_id, order_id):
    item   = request.form.get("order_item")
    amount = float(request.form.get("amount"))
    notes  = request.form.get("notes", "")

    date_str = request.form.get("delivery_date")
    time_str = request.form.get("delivery_time")
    delivery_datetime = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    delivery_datetime = delivery_datetime.replace(tzinfo=PH_TZ)

    try:
        orders.document(order_id).update({  # ← CHANGED
            "item": item,
            "amount": amount,
            "notes": notes,
            "delivery_date": delivery_datetime
        })
        log_admin_action(
            action="Edited order details",
            target=f"Order #{order_id} — {item}",
            category="order"
        )
        flash('Order updated successfully!', 'success')
    except Exception as e:
        flash(f'Error updating order: {str(e)}', 'error')

    return redirect(url_for("admin_orders"))

# ---------------- ADD INVENTORY ----------------
@app.route("/inventory/add", methods=["POST"])
@admin_required
def add_inventory():
    item     = request.form["item"]
    quantity = int(request.form["quantity"])
    cost     = float(request.form["cost"])
    now      = datetime.now(PH_TZ)

    # Add to inventory with timestamps
    inventory.add({
        "item": item,
        "quantity": quantity,
        "cost": cost,
        "created_at": now,
        "updated_at": now
    })
    
    # Add to expenses
    expenses.add({
        "description": f"Purchased {quantity} x {item}",
        "cost": cost * quantity,
        "date": now
    })
    
    log_admin_action(
        action="Added inventory item",
        target=f"{item} — qty: {quantity}, cost: ₱{cost}",
        category="inventory"
    )
    return redirect(url_for("admin_inventory"))

# ---------------- EDIT INVENTORY ----------------
@app.route("/inventory/edit/<id>", methods=["POST"])
@admin_required
def edit_inventory(id):
    now = datetime.now(PH_TZ)
    
    # Update inventory
    inventory.document(id).update({
        "item": request.form["item"],
        "quantity": int(request.form["quantity"]),
        "cost": float(request.form["cost"]),
        "updated_at": now
    })
    expenses.add({
        "description": f"Edited inventory: {request.form['item']} - Qty: {request.form['quantity']}, Cost: ₱{request.form['cost']}",
        "cost": float(request.form["cost"]) * int(request.form["quantity"]),
        "date": now
    })
    
    log_admin_action(
        action="Edited inventory item",
        target=request.form["item"],
        category="inventory"
    )
    
    return redirect(url_for("admin_inventory"))

# ---------------- EDIT EXPENSES COST----------------
@app.route("/expenses/edit/<id>", methods=["POST"])
@admin_required
def edit_expense(id):
    new_cost = float(request.form["cost"])
    
    expenses.document(id).update({
        "cost": new_cost
    })
    
    flash("Expense cost updated", "success")
    return redirect(url_for("admin_expenses"))
# ---------------- ADD CAKE ----------------
@app.route('/cake/add', methods=['POST'])
@admin_required
def add_cake():
    try:
        file = request.files.get('image')
        if file and file.filename:
            image_filename = save_uploaded_image(file, 'cake')
            if image_filename is None:
                flash('Image too large or invalid! Max 2MB.', 'danger')
                return redirect(url_for('admin_cakes'))
        else:
            image_filename = None

        cakes.add({
            'name':        request.form.get('name'),
            'description': request.form.get('description'),
            'category':    request.form.get('category'),
            'price':       float(request.form.get('price')),
            'quantity':    int(request.form.get('quantity')),
            'status':      request.form.get('status') == 'on',
            'image':       image_filename,
            'created_at':  datetime.now()
        })
        log_admin_action(
            action="Added new cake",
            target=request.form.get('name'),
            category="cake"
        )
        flash('Cake added!', 'success')
    except Exception as e:
        flash(f'Error: {str(e)}', 'danger')

    return redirect(url_for('admin_cakes'))

# ---------------- EDIT CAKE ----------------
@app.route('/cake/edit/<cake_id>', methods=['POST'])
@admin_required
def edit_cake(cake_id):
    try:
        cake_ref = cakes.document(cake_id)
        cake_doc = cake_ref.get()

        if not cake_doc.exists:
            flash('Cake not found!', 'danger')
            return redirect(url_for('admin_cakes'))

        cake_ref.update({
            'name':        request.form.get('name'),
            'description': request.form.get('description'),
            'category':    request.form.get('category'),
            'price':       float(request.form.get('price')),
            'quantity':    int(request.form.get('quantity')),
            'status':      request.form.get('status') == 'on',
            'image':       cake_doc.to_dict().get('image')
        })
        log_admin_action(
            action="Edited cake",
            target=f"{request.form.get('name')} (ID: {cake_id})",
            category="cake"
        )
        flash('Cake updated!', 'success')
    except Exception as e:
        flash(f'Error: {str(e)}', 'danger')

    return redirect(url_for('admin_cakes'))

# ---------------- DELETE CAKE ----------------
@app.route('/cake/delete/<cake_id>', methods=['POST'])
@admin_required
def delete_cake(cake_id):
    try:
        cake_ref = cakes.document(cake_id)
        cake_doc = cake_ref.get()

        if not cake_doc.exists:
            flash('Cake not found!', 'danger')
            return redirect(url_for('admin_cakes'))

        image_url = cake_doc.to_dict().get('image')
        if image_url and 'cloudinary.com' in image_url:
            public_id = '/'.join(image_url.split('/')[-3:]).rsplit('.', 1)[0]
            try:
                cloudinary.uploader.destroy(public_id)
            except Exception as e:
                print(f"Cloudinary delete error: {str(e)}")

        cake_name = cake_doc.to_dict().get('name', cake_id)  # add this BEFORE cake_ref.delete()
        cake_ref.delete()
        log_admin_action(
            action="Deleted cake",
            target=f"{cake_name} (ID: {cake_id})",
            category="cake"
        )
        flash('Cake deleted!', 'success')
    except Exception as e:
        flash(f'Error: {str(e)}', 'danger')

    return redirect(url_for('admin_cakes'))

# ---------------- DISABLE USER ----------------
@app.route('/admin/user/disable/<uid>', methods=['POST'])
@admin_required
def disable_user(uid):
    auth.update_user(uid, disabled=True)
    log_admin_action(
        action="Disabled user account",
        target=uid,
        category="user"
    )
    flash('User disabled!', 'warning')
    return redirect(url_for('admin_users'))

# ---------------- ENABLE USER ----------------
@app.route('/admin/user/enable/<uid>', methods=['POST'])
@admin_required
def enable_user(uid):
    auth.update_user(uid, disabled=False)
    log_admin_action(
        action="Enabled user account",
        target=uid,
        category="user"
    )
    flash('User enabled!', 'success')
    return redirect(url_for('admin_users'))

# ---------------- TOGGLE REVIEW VISIBILITY ----------------
@app.route("/admin/review/toggle/<review_id>", methods=["POST"])
@admin_required
def toggle_review(review_id):
    review_ref = reviews.document(review_id)
    review_doc = review_ref.get()
 
    if not review_doc.exists:
        flash("Review not found.", "danger")
        return redirect(url_for("admin_reviews"))
 
    current = review_doc.to_dict().get("is_visible", True)
    review_ref.update({"is_visible": not current})
    review_data = review_doc.to_dict()
    log_admin_action(
        action="Hid review" if current else "Made review visible",
        target=f"Review by {review_data.get('reviewer_name', '?')} on {review_data.get('cake_name', '?')}",
        category="review"
    )
    flash("Review visibility updated!", "success")
    return redirect(url_for("admin_reviews"))
# ================================================================
# NEW: /payment/success
# ================================================================
@app.route("/payment/success")
@login_required
def payment_success():
    session_id    = session.get('paymongo_session_id')
    pending_order = session.get('pending_order')
    
    if not session_id or not pending_order:
        flash("Invalid payment session.", "danger")
        return redirect(url_for("customer_dashboard"))
 
    payment_result = verify_payment(session_id)
 
    if not payment_result.get("paid"):
        flash("Payment not confirmed. Please try again.", "danger")
        return redirect(url_for("customer_dashboard"))
 
    user_id    = pending_order["user_id"]
    order_data = pending_order["order_data"]
 
    order_data["delivery_date"] = datetime.fromisoformat(order_data["delivery_date"])
    order_data["created_at"]    = datetime.fromisoformat(order_data["created_at"])
    order_data["payment_status"] = "Paid"
    order_data["payment_id"]     = payment_result.get("reference")
    order_data["payment_method"] = payment_result.get("payment_method", order_data["payment_method"]).upper()
 
    # Save to top-level orders collection
    doc_ref = orders.add(order_data)  # ← CHANGED
    order_id = doc_ref[1].id
 
    session.pop('paymongo_session_id', None)
    session.pop('pending_order', None)
 
    saved_order            = order_data.copy()
    saved_order["id"]      = order_id
    saved_order["user_id"] = user_id
 
    return render_template("payment_success.html",
        order          = saved_order,
        payment_result = payment_result
    )
 
 
# ================================================================
# NEW: /payment/failed
# ================================================================
@app.route("/payment/failed")
@login_required
def payment_failed():
    # Clear pending session
    session.pop('paymongo_session_id', None)
    session.pop('pending_order', None)
 
    flash("Payment was cancelled or failed. Please try again.", "danger")
    return redirect(url_for("cakes_page"))
# ================================================================
# CHATBOT ROUTES
# ================================================================

# ---------------- SEND MESSAGE ----------------
@app.route('/send-message', methods=['POST'])
def api_send_message():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        message = data.get('message', '').strip()
        conversation_id = data.get('conversation_id')
        is_escalation = data.get('is_escalation', False)
        
        if not user_id or not message:
            return jsonify({'success': False, 'error': 'Missing data'}), 400
        
        now = datetime.now(PH_TZ)
        
        # Ensure conversation document exists
        conv_ref = users.document(user_id).collection("conversations").document(conversation_id)
        conv_doc = conv_ref.get()
        
        if not conv_doc.exists:
            conv_ref.set({
                'created_at': now,
                'last_updated': now,
                'escalated': False  # New flag
            })
        
        # Get current conversation data
        conv_data = conv_doc.to_dict() if conv_doc.exists else {'escalated': False}
        is_escalated = conv_data.get('escalated', False)
        
        # If this is an escalation request, update the flag
        if is_escalation and not is_escalated:
            conv_ref.update({
                'escalated': True,
                'escalated_at': now,
                'escalated_by': 'customer'
            })
            is_escalated = True
        
        # Save customer message
        messages_ref = conv_ref.collection("messages")
        messages_ref.add({
            "text": message,
            "sender": "customer",
            "timestamp": now,
            "created_at": now
        })
        
        # Update last_updated
        conv_ref.update({'last_updated': now})
        
        # Only send bot response if conversation is NOT escalated
        if not is_escalated:
            if is_escalation:
                bot_response = "✅ Thank you! The shop owner has been notified and will respond shortly. You're now chatting with the owner."
            else:
                bot_response = get_faq_response(message)
            
            # Save bot response
            messages_ref.add({
                "text": bot_response,
                "sender": "bot",
                "timestamp": now,
                "created_at": now
            })
        else:
            # If escalated, don't send bot response
            if is_escalation:
                # Special message for escalation
                messages_ref.add({
                    "text": "✅ You're now connected with the shop owner. They'll respond shortly.",
                    "sender": "bot",
                    "timestamp": now,
                    "created_at": now
                })
        
        return jsonify({'success': True, 'escalated': is_escalated})
        
    except Exception as e:
        print(f"Error in send_message: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ---------------- ADMIN REPLY ----------------
@app.route('/admin/reply-message', methods=['POST'])
@admin_required
def admin_reply_message():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        conversation_id = data.get('conversation_id')
        message = data.get('message', '').strip()
        
        if not user_id or not message or not conversation_id:
            return jsonify({'success': False, 'error': 'Missing data'}), 400
        
        now = datetime.now(PH_TZ)
        
        # Ensure conversation exists and is escalated
        conv_ref = users.document(user_id).collection("conversations").document(conversation_id)
        conv_doc = conv_ref.get()
        
        if not conv_doc.exists:
            conv_ref.set({
                'created_at': now,
                'last_updated': now,
                'escalated': True,  # Admin replying escalates automatically
                'escalated_at': now,
                'escalated_by': 'admin'
            })
        else:
            # If conversation exists but not escalated, escalate it
            conv_data = conv_doc.to_dict()
            if not conv_data.get('escalated', False):
                conv_ref.update({
                    'escalated': True,
                    'escalated_at': now,
                    'escalated_by': 'admin'
                })
        
        # Save admin message
        conv_ref.collection("messages").add({
            "text": message,
            "sender": "admin",
            "timestamp": now,
            "created_at": now
        })
        
        # Update last_updated
        conv_ref.update({'last_updated': now})
        
        return jsonify({'success': True})
        
    except Exception as e:
        print(f"Error in admin_reply: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ---------------- GET CONVERSATION STATUS ----------------
@app.route('/conversation-status/<user_id>/<conversation_id>', methods=['GET'])
def get_conversation_status(user_id, conversation_id):
    try:
        conv_ref = users.document(user_id).collection("conversations").document(conversation_id)
        conv_doc = conv_ref.get()
        
        if not conv_doc.exists:
            return jsonify({'success': True, 'escalated': False})
        
        conv_data = conv_doc.to_dict()
        return jsonify({
            'success': True,
            'escalated': conv_data.get('escalated', False),
            'escalated_at': conv_data.get('escalated_at'),
            'escalated_by': conv_data.get('escalated_by')
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ---------------- ADMIN GET CONVERSATIONS ----------------
@app.route('/admin/conversations')
@admin_required
def admin_conversations():
    try:
        all_convos = []
        
        for user_doc in users.stream():
            user_data = user_doc.to_dict()
            if not user_data:
                continue
            
            # Get all conversations for this user
            conversations_ref = users.document(user_doc.id).collection("conversations").stream()
            
            for convo_doc in conversations_ref:
                try:
                    # Get conversation data FIRST
                    convo_data = convo_doc.to_dict()  # This is the fix - define conv_data here
                    
                    # Get messages
                    msgs = list(
                        users.document(user_doc.id)
                        .collection("conversations")
                        .document(convo_doc.id)
                        .collection("messages")
                        .order_by("timestamp")
                        .stream()
                    )
                    
                    if not msgs:
                        continue
                    
                    last = msgs[-1].to_dict()
                    last_msg = last.get("text", "")[:50] if last.get("text") else "No message"
                    ts = last.get("timestamp")
                    
                    if isinstance(ts, datetime):
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
                        else:
                            ts = ts.astimezone(PH_TZ)
                    
                    all_convos.append({
                        "user_id": user_doc.id,
                        "convo_id": convo_doc.id,
                        "customer_name": user_data.get("fname") or user_data.get("username", "Customer"),
                        "email": user_data.get("email", "No email"),
                        "last_message": last_msg,
                        "last_time": ts.strftime("%b %d %I:%M %p") if ts else "No messages",
                        "escalated": convo_data.get('escalated', False)  # Now convo_data exists
                    })
                    
                except Exception as e:
                    print(f"Error processing conversation {convo_doc.id}: {e}")
                    continue
        
        all_convos.sort(key=lambda x: x["last_time"] or "", reverse=True)
        
        return render_template("admin_conversations.html", conversations=all_convos)
        
    except Exception as e:
        print(f"Error in admin_conversations: {str(e)}")
        flash("Error loading conversations", "danger")
        return render_template("admin_conversations.html", conversations=[])
# ================================================================
# RUN SERVER
# ================================================================
if __name__ == "__main__":
    #indi pag kaksa ang comment pang live server lng na
    '''
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        ngrok.kill()
        public_url = ngrok.connect(5000)
        print(f"\n🌐 Public URL: {public_url}\n")
    '''
    app.run(debug=True)