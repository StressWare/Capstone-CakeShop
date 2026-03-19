from flask import Flask, render_template, request, redirect, url_for, session, flash,jsonify
from datetime import datetime, timedelta, timezone
from werkzeug.utils import secure_filename
import uuid
import os
import json
import cloudinary
import cloudinary.uploader
import firebase
from db import sales, expenses, inventory, users, cakes, walkin_orders # Firestore collections
from firebase_admin import auth, firestore
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY')
from pos import pos_bp
app.register_blueprint(pos_bp)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 # 2MB max file size
#cloud storage
cloudinary.config(
    cloud_name = os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key    = os.getenv('CLOUDINARY_API_KEY'),
    api_secret = os.getenv('CLOUDINARY_API_SECRET')
)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'heic'}
# Define Philippine Timezone (GMT+8)
PH_TZ = timezone(timedelta(hours=8))
#JSON
FAQ = {
    "how to order": "📝 To place an order with Ms. Brave Cake Shop:\n\n1. Click 'Order Now' on our homepage\n2. Select your cake design and flavor\n3. Choose delivery date and time\n4. Fill in recipient details\n5. Review and confirm your order\n6. Choose payment method\n\nThat's it! We'll confirm your order shortly.",
    
    "delivery time": "🚚 Our delivery times:\n\n⏱️ Standard Delivery: 2-3 business days\n⚡ Rush Delivery: 24 hours (available for ₱150 extra)\n\n📍 Delivery areas: Metro and nearby provinces\n🎁 Free delivery for orders ₱2000 and above\n\nDelivery is available 10 AM - 6 PM daily.",
    
    "customization": "🎨 Yes! We offer full customization:\n\n🍰 Flavors: Vanilla, Chocolate, Red Velvet, Ube, Strawberry, and more\n🧁 Frosting: Buttercream, Cream Cheese, Chocolate Ganache\n🎂 Design: Custom designs, personalized messages, themed decorations\n👶 Special requests: Sugar-free, dairy-free, vegan options available\n\nPlease mention your preferences in the order notes!",
    
    "payment methods": "💳 We accept multiple payment methods:\n\n💵 Cash on Delivery (COD)\n📱 GCash & PayMaya\n🏦 Bank Transfer (BPI, BDO, Metrobank)\n💰 Online Payment (Debit/Credit Card)\n\nPayment must be settled before delivery. We send a QR code or bank details after confirmation.",
    
    "return policy": "🔄 Return & Refund Policy:\n\n❌ Non-returnable items: Baked goods due to perishability\n✅ Refund eligibility: Only if cake is damaged or incorrect upon delivery\n🕐 Timeline: Report issues within 24 hours of delivery\n💰 Refund process: Full refund or replacement (customer's choice)\n\nPlease message us immediately with photos if there's an issue!",
    
    "default": "😊 I'm not sure about that question. Please click one of the FAQ buttons above or contact the owner directly using the 'Chat with Owner' button. Thank you!"
}
#bot function
def get_faq_response(user_message):
    """
    Match user message to FAQ and return response
    Uses simple keyword matching
    """
    user_message_lower = user_message.lower()
    
    # Check each FAQ keyword
    for faq_key, faq_answer in FAQ.items():
        if faq_key != "default":
            # Split key into keywords
            keywords = faq_key.split()
            # Check if any keyword matches
            if any(keyword in user_message_lower for keyword in keywords):
                return faq_answer
    
    # If no match found, return default response
    return FAQ.get("default", "I'm not sure. Please contact us directly!")

# ------ SECURED FILE HANDLING FUNC-----
def save_uploaded_image(file, upload_type):
    ext = file.filename.rsplit('.', 1)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return None

    # Check file size
    file.seek(0, 2)
    file_size = file.tell()
    file.seek(0)

    if file_size > 2 * 1024 * 1024:  # 2MB
        return None

    # Cloudinary upload
    if upload_type == 'cake':
        folder = 'cake_shop/cakes'
    else:
        folder = 'cake_shop/orders'

    try:
        result = cloudinary.uploader.upload(
            file,
            folder = folder,
            resource_type = 'image'
        )
        return result['secure_url']
    except Exception as e:
        print(f"Cloudinary upload error: {str(e)}")
        return None
#ROUTE START
# ---------------- HOME PAGE ----------------
@app.route("/")
def home_page():
    # Check if user is logged in
    user_id = session.get("user_id")
    customer = None
    
    if user_id:
        doc = users.document(user_id).get()
        if doc.exists:
            customer = doc.to_dict()
    
    return render_template("home.html", customer=customer)

# Customization page
@app.route('/customize_cake')
def customize():
    user_id = session.get('user_id')
    customer = None
    if user_id:  
        doc = users.document(user_id).get()
        if doc.exists:
            customer = doc.to_dict()
    return render_template('customization.html', customer=customer)

# ---------------- LOGIN / SIGNUP ----------------
@app.route("/authentication")
def auth_page():
    return render_template("authentication.html")

# ---------------- LOGOUT ----------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth_page"))

@app.route('/forgot-password')
def forgot_password_page():
    return render_template('forgot_password.html')

# ------------------- VERIFY TOKEN (called after Firebase login) -------------------
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
            return jsonify({
                'error': 'Please verify your email first!',
                'needs_verification': True
            }), 401
        # Create Firestore document if it doesn't exist (for Google Sign-In)
        user_doc = users.document(uid).get()
        is_new_user = not user_doc.exists  # ← ADD THIS

        if not user_doc.exists:
            users.document(uid).set({
                'email': email,
                'username': email.split('@')[0] if email else '',
                'fname': '',
                'number': '',
                'address': '',
                'role': 'customer',
                'created_at': firestore.SERVER_TIMESTAMP
            })

        # Check for admin custom claim (optional)
        is_admin = decoded_token.get('admin', False)

        # Set session
        session['user'] = {
            'uid': uid,
            'email': email,
            'admin': is_admin
        }
        session['user_id'] = uid
        session['username'] = email

        return jsonify({'success': True, 'needs_profile': is_new_user, 'is_admin': is_admin}), 200  

    except auth.InvalidIdTokenError:
        return jsonify({'error': 'Invalid token'}), 401
    except auth.ExpiredIdTokenError:
        return jsonify({'error': 'Token expired'}), 401
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ------------------- SAVE EXTRA USER DETAILS (after email/password signup) -------------------
@app.route('/save-user-details', methods=['POST'])
def save_user_details():
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'Missing or invalid token'}), 401

    id_token = auth_header.split('Bearer ')[1]

    try:
        # Verify token to ensure it's the same user
        decoded_token = auth.verify_id_token(id_token)
        token_uid = decoded_token['uid']

        data = request.get_json()
        uid = data.get('uid')
        if uid != token_uid:
            return jsonify({'error': 'UID mismatch'}), 403

        # Extract fields (must match HTML input IDs)
        username = data.get('username')
        number = data.get('number')
        address = data.get('address')
        fname = data.get('fname')

        # Update Firestore (use 'number', not 'phone')
        user_ref = users.document(uid)
        user_ref.set({
            'username': username,
            'number': number,
            'address': address,
            'fname': fname,
            'email': decoded_token.get('email', ''),
            'role': 'customer',
            'created_at': firestore.SERVER_TIMESTAMP
        })

        return jsonify({'success': True}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/complete-profile', methods=['GET', 'POST'])
def complete_profile():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('auth_page'))

    if request.method == 'POST':
        users.document(user_id).update({
            'fname': request.form.get('fname'),
            'username': request.form.get('username'),
            'number': request.form.get('number'),
            'address': request.form.get('address'),
        })
        return redirect(url_for('customer_dashboard'))

    # GET - just show the form
    doc = users.document(user_id).get()
    customer = doc.to_dict()
    return render_template('complete_profile.html', customer=customer)

# ---------------- COMBINED ADMIN (DASHBOARD + PANEL) ----------------
@app.route("/admin_dashboard")
def admin_page():
    current_user = session.get('user')
     # Not logged in at all
    if not current_user:
        return redirect(url_for('auth_page'))  # 401 situation
    
    # Logged in but not admin
    if not current_user.get('admin'):
        return render_template('403.html'), 403  # 403 situation
    # Fetch inventory
    inv_items = []
    for doc in inventory.stream():
        item = doc.to_dict()
        item["id"] = doc.id
        inv_items.append(item)

    # Fetch expenses
    exp_items = []
    for doc in expenses.stream():
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

    # Fetch sales (Completed/Pickup orders only)
    sales_items = []
    for user_doc in users.stream():
        user_data = user_doc.to_dict()
        orders_ref = users.document(user_doc.id).collection("orders").stream()
        for order_doc in orders_ref:
            order = order_doc.to_dict()
            if order.get("status") in ["Completed", "Pickup"]:
                order["customer_username"] = user_data.get("username", "")
                order["id"] = order_doc.id
                created_at = order.get("created_at")
                if isinstance(created_at, str):
                    created_at = datetime.fromisoformat(created_at)
                if isinstance(created_at, datetime):
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
                    else:
                        created_at = created_at.astimezone(PH_TZ)
                order["created_at"] = created_at
                sales_items.append(order)

    # Weekly calculations
    now = datetime.now(PH_TZ)
    week_ago = now - timedelta(days=7)
    days_order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    weekly_sales = {day: 0 for day in days_order}
    weekly_expenses = {day: 0 for day in days_order}
    weekly_profit = {day: 0 for day in days_order}

    for e in exp_items:
        e_date = e.get("date")
        if isinstance(e_date, datetime) and e_date >= week_ago:
            weekly_expenses[e_date.strftime("%a")] += float(e.get("cost", 0))

    for s in sales_items:
        s_date = s.get("created_at")
        if isinstance(s_date, datetime) and s_date >= week_ago:
            weekly_sales[s_date.strftime("%a")] += float(s.get("amount", 0))

    for day in days_order:
        weekly_profit[day] = weekly_sales[day] - weekly_expenses[day]

    # =============== PANEL DATA ===============
    low_stock = [doc.to_dict() for doc in inventory.where("quantity", "<", 10).stream()]

    orders = []
    for user_doc in users.stream():
        user_data = user_doc.to_dict()
        orders_ref = users.document(user_doc.id).collection("orders").order_by("delivery_date").stream()
        for order_doc in orders_ref:
            order = order_doc.to_dict()
            order["id"] = order_doc.id
            order["user_id"] = user_doc.id
            order["notes"] = order.get("notes", "")
            order["inspo_image"] = order.get("inspo_image", None)
            order["customer_username"] = user_data.get("username", "")

            if isinstance(order.get("delivery_date"), str):
                order["delivery_date"] = datetime.fromisoformat(order["delivery_date"])
            if isinstance(order.get("delivery_date"), datetime):
                if order["delivery_date"].tzinfo is None:
                    order["delivery_date"] = order["delivery_date"].replace(tzinfo=timezone.utc).astimezone(PH_TZ)
                else:
                    order["delivery_date"] = order["delivery_date"].astimezone(PH_TZ)
            
            if isinstance(order.get("created_at"), str):
                order["created_at"] = datetime.fromisoformat(order["created_at"])
            if isinstance(order.get("created_at"), datetime):
                if order["created_at"].tzinfo is None:
                    order["created_at"] = order["created_at"].replace(tzinfo=timezone.utc).astimezone(PH_TZ)
                else:
                    order["created_at"] = order["created_at"].astimezone(PH_TZ)

            orders.append(order)
    orders.sort(key=lambda x: x["created_at"] or datetime.min.replace(tzinfo=PH_TZ), reverse=True)
    all_users = []
    for user_doc in users.stream():
        user_data = user_doc.to_dict()
        user_data['uid'] = user_doc.id
        
        # count orders
        order_count = len(list(users.document(user_doc.id).collection('orders').stream()))
        user_data['order_count'] = order_count
        
        # get Firebase Auth info (verified, disabled)
        try:
            auth_user = auth.get_user(user_doc.id)
            user_data['disabled'] = auth_user.disabled
            user_data['email_verified'] = auth_user.email_verified
            created_ms = auth_user.user_metadata.creation_timestamp
            user_data['created_at'] = datetime.fromtimestamp(created_ms / 1000, tz=PH_TZ)
        except:
            user_data['disabled'] = False
            user_data['email_verified'] = False
            user_data['created_at'] = None
        
        all_users.append(user_data)
    # Order statistics
    total_new = 0
    total_accepted = 0
    total_pending = 0
    total_ready = 0
    total_out = 0
    total_completed = 0
    total_cancelled = 0
    total_rush = 0
    today_count = 0
    today_deliveries = []
    today_date = datetime.now(PH_TZ).date()

    for order in orders:
        status = order.get("status", "")
        
        if status == "New":
            total_new += 1
        elif status == "Accepted":
            total_accepted += 1
        elif status == "Pending":
            total_pending += 1
        elif status == "Ready":
            total_ready += 1
        elif status == "Out for Delivery":
            total_out += 1
        elif status == "Completed":
            total_completed += 1
        elif status == "Cancelled":
            total_cancelled += 1
        
        if order.get("rush"):
            total_rush += 1
        
        delivery_date = order.get("delivery_date")
        if isinstance(delivery_date, datetime):
            if delivery_date.date() == today_date:
                if status not in ["Completed", "Cancelled"]:
                    today_count += 1
                    today_deliveries.append({
                        "time": delivery_date.strftime("%I:%M %p"),
                        "customer": order.get("customer", {}).get("name", "N/A"),
                        "cake": order.get("item", "N/A"),
                        "status": status,
                        "rush": order.get("rush", False)
                    })

    today_deliveries.sort(key=lambda x: datetime.strptime(x["time"], "%I:%M %p"))

    all_cakes = cakes.stream()
    cakes_list = []
    for cake in all_cakes:
        cake_data = cake.to_dict()
        cake_data['id'] = cake.id
        cakes_list.append(cake_data)
    
    # =============== RENDER ===============
    return render_template(
        "admin_dashboard.html",
        # DASHBOARD data
        inventory=inv_items,
        expenses=exp_items,
        sales=sales_items,
        weekly_sales=weekly_sales,
        weekly_expenses=weekly_expenses,
        weekly_profit=weekly_profit,
        week_ago=week_ago,
        
        # PANEL data
        orders=orders,
        low_stock=low_stock,
        total_new=total_new,
        total_accepted=total_accepted,
        total_pending=total_pending,
        total_ready=total_ready,
        total_out=total_out,
        total_completed=total_completed,
        total_cancelled=total_cancelled,
        total_rush=total_rush,
        today_count=today_count,
        today_deliveries=today_deliveries,
        cakes=cakes_list,
        all_users=all_users
    )
@app.route('/admin/user/disable/<uid>', methods=['POST'])
def disable_user(uid):
    current_user = session.get('user')
    if not current_user or not current_user.get('admin'):
        return render_template('403.html'), 403
    auth.update_user(uid, disabled=True)
    flash('User disabled!', 'warning')
    return redirect(url_for('admin_page') + '#users')

@app.route('/admin/user/enable/<uid>', methods=['POST'])
def enable_user(uid):
    current_user = session.get('user')
    if not current_user or not current_user.get('admin'):
        return render_template('403.html'), 403
    auth.update_user(uid, disabled=False)
    flash('User enabled!', 'success')
    return redirect(url_for('admin_page') + '#users')
# ---------------- UPDATE ORDER STATUS ----------------
@app.route("/order/status/<user_id>/<order_id>", methods=["POST"])
def update_order_status(user_id, order_id):
    current_user = session.get('user')
    if not current_user or not current_user.get('admin'):
        return render_template('403.html'), 403

    new_status = request.form["status"]

    order_ref = users.document(user_id).collection("orders").document(order_id)
    order_doc = order_ref.get()

    if order_doc.exists:
        order_data = order_doc.to_dict()
        old_status = order_data.get("status")
        order_type = order_data.get("order_type", "custom")

        # Decrease quantity when admin accepts premade order
        if new_status == "Accepted" and old_status == "New" and order_type == "premade":
            selected_items = order_data.get("selected_items", [])
            for i in selected_items:
                cake_ref = cakes.document(i["cake_id"])
                cake_doc = cake_ref.get()
                if cake_doc.exists:
                    current_qty = cake_doc.to_dict().get("quantity", 0)
                    new_qty = max(0, current_qty - 1)
                    cake_ref.update({
                        "quantity": new_qty,
                        "status": new_qty > 0
                    })

        # Restore quantity when admin cancels accepted premade order
        accepted_statuses = ["Accepted", "Pending", "Ready", "Out for Delivery"]
        if new_status == "Cancelled" and old_status in accepted_statuses and order_type == "premade":
            selected_items = order_data.get("selected_items", [])
            for i in selected_items:
                cake_ref = cakes.document(i["cake_id"])
                cake_doc = cake_ref.get()
                if cake_doc.exists:
                    current_qty = cake_doc.to_dict().get("quantity", 0)
                    cake_ref.update({
                        "quantity": current_qty + 1,
                        "status": True
                    })

    order_ref.update({"status": new_status})
    return redirect(url_for("admin_page"))


# ---------------- ADD INVENTORY ----------------
@app.route("/inventory/add", methods=["POST"])
def add_inventory():
    current_user = session.get('user')
    if not current_user or not current_user.get('admin'):
        return render_template('403.html'), 403
    
    item = request.form["item"]
    quantity = int(request.form["quantity"])
    cost = float(request.form["cost"])

    inventory.add({
        "item": item,
        "quantity": quantity,
        "cost": cost
    })

    expenses.add({
        "description": item,
        "cost": cost,
        "date": datetime.now(PH_TZ)  # This is correct - PH time
    })

    return redirect(url_for("admin_page"))


# ---------------- EDIT INVENTORY ----------------
@app.route("/inventory/edit/<id>", methods=["POST"])
def edit_inventory(id):
    current_user = session.get('user')
    if not current_user or not current_user.get('admin'):
        return render_template('403.html'), 403

    inventory.document(id).update({
        "item": request.form["item"],
        "quantity": int(request.form["quantity"]),
        "cost": float(request.form["cost"])
    })
    return redirect(url_for("admin_page"))


# ---------------- CUSTOMIZATION ORDER ----------------
@app.route("/order", methods=["POST"])
def place_order():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth_page"))

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
        order_type  = 'custom',
        order_item  = request.form.get('order_item'),
        amount      = request.form.get('amount'),
        notes       = request.form.get('notes', ''),
        rush        = request.form.get('rush', ''),
        inspo_image = inspo_image,
        selected_items = [],
        customer    = customer,
        min_date    = min_date,
    )

#PREMADE ORDERS
@app.route("/order/cake", methods=["POST"])
def order_cake():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth_page"))

    customer_doc = users.document(user_id).get()
    if customer_doc.exists:
        customer = customer_doc.to_dict()
    else:
        customer = {}
    min_date = (datetime.now(PH_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")

    # From cart (multiple items)
    selected_json  = request.form.get('selected_items', '[]')
    selected_items = json.loads(selected_json)
    print("selected_items being passed to template:", selected_items)
    if selected_items:
        amount = sum(float(i['price']) for i in selected_items)
    else:
        # From Order Now (single cake)
        selected_items = [{
            'cake_id':   request.form.get('cake_id'),
            'cake_name': request.form.get('cake_name'),
            'price':     request.form.get('price')
        }]
        amount = float(request.form.get('price', 0))

    return render_template('checkout.html',
        order_type     = 'premade',
        selected_items = selected_items,
        amount         = amount,
        customer       = customer,
        min_date       = min_date,
    )
#CHECKOUT PAGE FOR both custom and premade orders
@app.route('/checkout')
def checkout_page():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('auth_page'))
    return redirect(url_for('cakes_page'))

# ---------------- /place-order - SAVES TO FIRESTORE (from checkout form) ----------------
@app.route("/place-order", methods=["POST"])
def finalize_order():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth_page"))
    now = datetime.now(PH_TZ)

    date_str = request.form["delivery_date"]
    time_str = request.form["delivery_time"]
    delivery_datetime = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    delivery_datetime = delivery_datetime.replace(tzinfo=PH_TZ)

    delivery_type = request.form.get("delivery_type", "Delivery")

    if delivery_type == "Pickup":
        address = "Pick Up at Shop"
    else:
        address = request.form.get("address", "")

    order_type = request.form.get("order_type")
    selected_json = request.form.get("selected_items", "[]")
    print("EXACT VALUE:", repr(selected_json))
    if order_type == "premade":
        selected_json  = request.form.get("selected_items", "[]")
        selected_items = json.loads(selected_json)
        item_names     = ", ".join([f"{i['cake_name']} (₱{float(i['price']):.0f})" for i in selected_items])
        amount         = sum(float(i["price"]) for i in selected_items)
        rush           = False
        inspo_image    = None
        
        # Clear ordered items from cart
        for i in selected_items:
            users.document(user_id).collection("cart").document(i["cake_id"]).delete()

    else:  # custom
        item_names  = request.form.get("order_item", "")
        amount      = float(request.form.get("amount", 0))
        rush        = request.form.get("rush") == "yes"
        inspo_image = request.form.get("inspo_image") or None

    order_data = {
        "delivery_date":  delivery_datetime,
        "item":           item_names,
        "selected_items": selected_items,
        "amount":         amount,
        "status":         "New",
        "rush":           rush,
        "notes":          request.form.get("notes", ""),
        "payment_method": request.form.get("payment_method", "Cash on Delivery"),
        "delivery_type":  delivery_type,
        "inspo_image":    inspo_image,
        "order_type":     order_type,
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

    users.document(user_id).collection("orders").add(order_data)
    flash("Order placed successfully! 🎂", "success")
    return redirect(url_for("customer_dashboard"))

#CUSTOMER CANCEL ORDER
@app.route("/order/cancel/<order_id>", methods=["POST"])
def cancel_order(order_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth_page"))

    order_ref = users.document(user_id).collection("orders").document(order_id)
    order_doc = order_ref.get()

    if not order_doc.exists:
        flash("Order not found.", "danger")
        return redirect(url_for("customer_dashboard"))

    order_data = order_doc.to_dict()
    if order_data.get("status") != "New":
        flash("Order cannot be cancelled anymore.", "warning")
        return redirect(url_for("customer_dashboard"))

    order_ref.update({"status": "Cancelled"})
    flash("Order cancelled successfully.", "info")
    return redirect(url_for("customer_dashboard"))
# ---------------- CUSTOMER DASHBOARD ----------------
@app.route("/customer_dashboard")
def customer_dashboard():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth_page"))

    doc = users.document(user_id).get()
    if not doc.exists:
        return "User not found", 404
    customer = doc.to_dict()

    orders_ref = users.document(user_id).collection("orders").stream()
    orders = []
    for order_doc in orders_ref:
        order = order_doc.to_dict()
        order["id"] = order_doc.id
        order["notes"] = order.get("notes", "")

        created_at = order.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
            else:
                created_at = created_at.astimezone(PH_TZ)
        order["created_at"] = created_at

        delivery_date = order.get("delivery_date")
        if isinstance(delivery_date, str):
            delivery_date = datetime.fromisoformat(delivery_date)
        if isinstance(delivery_date, datetime):
            if delivery_date.tzinfo is None:
                delivery_date = delivery_date.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
            else:
                delivery_date = delivery_date.astimezone(PH_TZ)
        order["delivery_date"] = delivery_date

        orders.append(order)

    orders.sort(key=lambda x: x["created_at"], reverse=True)
    cart_count = len(list(users.document(user_id).collection("cart").stream()))
    return render_template(
        "customer_dashboard.html",
        customer=customer,
        orders=orders,
        user_id=user_id,
        cart_count=cart_count
    )

# ---------------- CUSTOMER PROFILE EDIT ----------------
@app.route("/customer/edit", methods=["POST"])
def edit_customer_profile():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth_page"))

    updated_data = {
        "username": request.form.get("username"),
        "number": request.form.get("contact"),
        "address": request.form.get("address"),
        "fname": request.form.get("full_name")
    }

    users.document(user_id).update(updated_data)
    return redirect(url_for("customer_dashboard"))

@app.route('/cake/add', methods=['POST'])
def add_cake():
    current_user = session.get('user')
    if not current_user or not current_user.get('admin'):
        return render_template('403.html'), 403
    
    try:
        name = request.form.get('name')
        description = request.form.get('description')
        category = request.form.get('category')
        price = float(request.form.get('price'))
        quantity = int(request.form.get('quantity'))
        status = request.form.get('status') == 'on'
        
        # Handle image
        file = request.files.get('image')
        if file and file.filename:
            image_filename = save_uploaded_image(file, 'cake')
            if image_filename is None:
                flash('Image too large or invalid! Max 2MB.', 'danger')
                return redirect('/admin_dashboard#cake-availability')
        else:
            image_filename = None

        
        # Save to Firestore
        cakes.add({
            'name': name,
            'description': description,
            'category': category,
            'price': price,
            'quantity': quantity,
            'status': status,
            'image': image_filename,
            'created_at': datetime.now()
        })
        
        flash('Cake added!', 'success')
    except Exception as e:
        flash(f'Error: {str(e)}', 'danger')
    
    return redirect('/admin_dashboard#cake-availability')


@app.route('/cake/edit/<cake_id>', methods=['POST'])
def edit_cake(cake_id):
    current_user = session.get('user')
    if not current_user or not current_user.get('admin'):
        return render_template('403.html'), 403

    try:
        cake_ref = cakes.document(cake_id)
        cake_doc = cake_ref.get()
        
        if not cake_doc.exists:
            flash('Cake not found!', 'danger')
            return redirect('/admin_panel#cake-availability')
        
        current_data = cake_doc.to_dict()
        
        name = request.form.get('name')
        description = request.form.get('description')
        category = request.form.get('category')
        price = float(request.form.get('price'))
        quantity = int(request.form.get('quantity'))
        status = request.form.get('status') == 'on'
        
        # Handle image
        image_filename = current_data.get('image')
        
        # Update Firestore
        cake_ref.update({
            'name': name,
            'description': description,
            'category': category,
            'price': price,
            'quantity': quantity,
            'status': status,
            'image': image_filename
        })
        
        flash('Cake updated!', 'success')
    except Exception as e:
        flash(f'Error: {str(e)}', 'danger')
    
    return redirect('/admin_dashboard#cake-availability')


@app.route('/cake/delete/<cake_id>', methods=['POST'])
def delete_cake(cake_id):
    current_user = session.get('user')
    if not current_user or not current_user.get('admin'):
        return render_template('403.html'), 403
 
    try:
        cake_ref = cakes.document(cake_id)
        cake_doc = cake_ref.get()
 
        if not cake_doc.exists:
            flash('Cake not found!', 'danger')
            return redirect('/admin_dashboard#cake-availability')
 
        cake_data = cake_doc.to_dict()
        image_url = cake_data.get('image')
 
        # Delete from Cloudinary if image exists
        if image_url and 'cloudinary.com' in image_url:
            # Extract public_id from URL
            # URL format: .../cake_shop/cakes/filename
            public_id = '/'.join(image_url.split('/')[-3:]).rsplit('.', 1)[0]
            try:
                cloudinary.uploader.destroy(public_id)
            except Exception as e:
                print(f"Cloudinary delete error: {str(e)}")
 
        # Delete from Firestore
        cake_ref.delete()
        flash('Cake deleted!', 'success')
 
    except Exception as e:
        flash(f'Error: {str(e)}', 'danger')
 
    return redirect('/admin_dashboard#cake-availability')
 

@app.route("/cakes")
def cakes_page():
    """Display all available cakes"""
    # Get available cakes (status = True only)
    available_cakes = []
    for cake_doc in cakes.where("status", "==", True).stream():
        cake_data = cake_doc.to_dict()
        cake_data['id'] = cake_doc.id
        available_cakes.append(cake_data)
    # Check if user is logged in
    user_id = session.get("user_id")
    
    return render_template("cakes.html", cakes=available_cakes, user_id=user_id)

# ---------------- CART PAGE ----------------
@app.route("/cart")
def cart_page():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth_page"))

    cart_items = []
    for doc in users.document(user_id).collection("cart").stream():
        item = doc.to_dict()
        item["id"] = doc.id
        cart_items.append(item)

    return render_template("cart.html", cart_items=cart_items)

# ---------------- ADD TO CART ----------------
@app.route("/cart/add", methods=["POST"])
def add_to_cart():
    user_id = session.get("user_id")
    if not user_id:
        flash("Please login first!", "warning")
        return redirect(url_for("auth_page"))

    cake_id   = request.form.get("cake_id")
    cake_name = request.form.get("cake_name")
    price     = float(request.form.get("price", 0))

    cart_ref = users.document(user_id).collection("cart").document(cake_id)
    cart_doc = cart_ref.get()

    if cart_doc.exists:
        flash(f"{cake_name} is already in your cart!", "info")
    else:
        cart_ref.set({
            "cake_id":   cake_id,
            "cake_name": cake_name,
            "price":     price,
            "added_at":  firestore.SERVER_TIMESTAMP
        })
        flash(f"{cake_name} added to cart! 🛒", "success")

    return redirect(url_for("cakes_page"))


# ---------------- REMOVE FROM CART ----------------
@app.route("/cart/remove/<cake_id>", methods=["POST"])
def remove_from_cart(cake_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth_page"))

    users.document(user_id).collection("cart").document(cake_id).delete()
    flash("Item removed from cart.", "info")
    return redirect(url_for("cart_page"))




@app.route('/api/send-message', methods=['POST'])
def api_send_message():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        message = data.get('message', '').strip()
        conversation_id = data.get('conversation_id')

        if not user_id or not message:
            return jsonify({'success': False, 'error': 'Missing data'}), 400

        # Get current time in PH timezone
        now = datetime.now(PH_TZ)

        # Save customer message to Firestore
        users.document(user_id).collection("conversations").document(conversation_id).collection("messages").add({
            "text": message,
            "sender": "customer",
            "timestamp": now,
            "created_at": now
        })

        # Get bot response (match FAQ keywords)
        bot_response = get_faq_response(message)

        # Save bot response to Firestore
        users.document(user_id).collection("conversations").document(conversation_id).collection("messages").add({
            "text": bot_response,
            "sender": "bot",
            "timestamp": now,
            "created_at": now
        })

        # Return response to frontend
        return jsonify({
            'success': True,
            'response': bot_response,
            'timestamp': now.isoformat()
        })

    except Exception as e:
        print(f"Error in send_message: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/messages/<user_id>/<conversation_id>', methods=['GET'])
def api_get_messages(user_id, conversation_id):
    """
    Retrieve all messages for a conversation
    """
    try:
        messages = []
        
        # Query all messages from the conversation
        messages_ref = users.document(user_id).collection("conversations").document(conversation_id).collection("messages").order_by("timestamp").stream()

        for msg_doc in messages_ref:
            msg = msg_doc.to_dict()
            # Convert timestamp to ISO format if it's a datetime object
            if isinstance(msg.get('timestamp'), datetime):
                timestamp = msg['timestamp']
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
                else:
                    timestamp = timestamp.astimezone(PH_TZ)
                msg['timestamp'] = timestamp.isoformat()
            
            messages.append({
                'text': msg.get('text', ''),
                'sender': msg.get('sender', 'unknown'),
                'timestamp': msg.get('timestamp', '')
            })

        return jsonify({
            'success': True,
            'messages': messages
        })

    except Exception as e:
        print(f"Error in get_messages: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

# Customization page
'''
@app.route('/checkout')
def checkout_page():
    user_id = session.get('user_id')
    customer = None
    if user_id:  
        doc = users.document(user_id).get()
        if doc.exists:
            customer = doc.to_dict()
    return render_template('checkout.html', customer=customer)
'''
# ---------------- RUN SERVER ----------------
if __name__ == "__main__":
    app.run(debug=True)