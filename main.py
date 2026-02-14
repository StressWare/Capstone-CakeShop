from flask import Flask, render_template, request, redirect, url_for, session, flash,jsonify
from datetime import datetime, timedelta, timezone
from werkzeug.utils import secure_filename
import uuid
import os
import firebase
from db import sales, expenses, inventory, users, cakes  # Firestore collections

app = Flask(__name__)
app.secret_key = "secretngani"

UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
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
def save_uploaded_image(file):
    """Helper function to save image with secure unique filename"""
    # Get file extension (jpg, png, etc)
    ext = file.filename.rsplit('.', 1)[1].lower()
        # Check if extension is allowed
    if ext not in ALLOWED_EXTENSIONS:
        return None  # Not an image, reject it
    
    # Make filename safe and unique
    safe_name = secure_filename(file.filename.rsplit('.', 1)[0])
    unique_id = uuid.uuid4().hex[:8]
    final_filename = f"{safe_name}-{unique_id}.{ext}"
    
    # Save the file
    file.save(os.path.join(app.config['UPLOAD_FOLDER'], final_filename))
    
    # Return the filename to save in database
    return final_filename
#ROUTE START
# ---------------- HOME PAGE ----------------
@app.route("/")
def home_page():
    # Check if user is logged in
    user_id = session.get("user_id")
    customer = None
    
    if user_id:
        # Get customer data from Firestore
        doc = users.document(user_id).get()
        if doc.exists:
            customer = doc.to_dict()
    
    return render_template("home.html", customer=customer)

# ---------------- LOGIN / SIGNUP ----------------
@app.route("/authentication", methods=["GET", "POST"])
def auth():
    message = ""

    if request.method == "POST":
        action = request.form.get("action")
        username = request.form.get("username")
        password = request.form.get("password")
        number = request.form.get("number")
        address = request.form.get("address")
        fname = request.form.get("fname")

        if action == "login":
            user_query = users.where("username", "==", username)\
                              .where("password", "==", password)\
                              .limit(1).stream()

            user = None
            for doc in user_query:
                user = doc.to_dict()
                session["user_id"] = doc.id
                session["username"] = username

            if user:
                return redirect(url_for("customer_dashboard"))
            else:
                message = "Invalid username or password."

        elif action == "signup":
            exists_query = users.where("username", "==", username).limit(1).stream()
            exists = any(exists_query)
            if exists:
                message = "Username already exists."
            else:
                doc_ref = users.add({
                    "username": username,
                    "password": password,
                    "number": number,
                    "address": address,
                    "fname": fname
                })
                session["user_id"] = doc_ref[1].id
                session["username"] = username
                return redirect(url_for("customer_dashboard"))

    return render_template("authentication.html", message=message)


# ---------------- LOGOUT ----------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth"))


# ---------------- COMBINED ADMIN (DASHBOARD + PANEL) ----------------
@app.route("/admin_dashboard")
def admin_page():
    # =============== DASHBOARD DATA ===============
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

        cakes=cakes_list
    )

# ---------------- UPDATE ORDER STATUS ----------------
@app.route("/order/status/<user_id>/<order_id>", methods=["POST"])
def update_order_status(user_id, order_id):
    new_status = request.form["status"]
    users.document(user_id).collection("orders").document(order_id).update({
        "status": new_status
    })
    return redirect(url_for("panel_page"))


# ---------------- ADD INVENTORY ----------------
@app.route("/inventory/add", methods=["POST"])
def add_inventory():
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
    inventory.document(id).update({
        "item": request.form["item"],
        "quantity": int(request.form["quantity"]),
        "cost": float(request.form["cost"])
    })
    return redirect(url_for("admin_page"))


# ---------------- PLACE ORDER ----------------
@app.route("/order", methods=["POST"])
def place_order():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth"))

    now = datetime.now(PH_TZ)

    # Get date and time from form
    date_str = request.form["delivery_date"]
    time_str = request.form["delivery_time"]
    datetime_str = f"{date_str} {time_str}"
    
    delivery_datetime = datetime.strptime(datetime_str, "%Y-%m-%d %H:%M")
    delivery_datetime = delivery_datetime.replace(tzinfo=PH_TZ)

    order_data = {
        "delivery_date": delivery_datetime,
        "item": request.form["order_item"],
        "amount": float(request.form["amount"]),
        "status": "New",
        "rush": bool(request.form.get("rush")),
        "notes": request.form.get("notes", ""),  # ✅ NEW - Special instructions
        "customer": {
            "name": request.form["customer_name"],
            "contact": request.form["contact"],
            "address": request.form["address"],
            "occasion": request.form["occasion"],
            "celebrant": request.form.get("celebrant"),
            "age": request.form.get("age")
        },
        "created_at": now
    }

    users.document(user_id).collection("orders").add(order_data)
    return redirect(url_for("customer_dashboard"))

# ---------------- CUSTOMER DASHBOARD ----------------
@app.route("/customer_dashboard")
def customer_dashboard():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth"))

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
        # Convert created_at
        created_at = order.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        
        # Convert to PH time
        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
            else:
                created_at = created_at.astimezone(PH_TZ)
        order["created_at"] = created_at
        
        # Convert delivery_date
        delivery_date = order.get("delivery_date")
        if isinstance(delivery_date, str):
            delivery_date = datetime.fromisoformat(delivery_date)
        
        # Convert to PH time
        if isinstance(delivery_date, datetime):
            if delivery_date.tzinfo is None:
                delivery_date = delivery_date.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
            else:
                delivery_date = delivery_date.astimezone(PH_TZ)
        order["delivery_date"] = delivery_date
        
        orders.append(order)

    # Sort descending by timestamp - SIMPLIFIED!
    orders.sort(key=lambda x: x["created_at"], reverse=True)

    return render_template(
        "customer_dashboard.html",
        customer=customer,
        orders=orders,
        user_id=user_id
    )


# ---------------- CUSTOMER PROFILE EDIT ----------------
@app.route("/customer/edit", methods=["POST"])
def edit_customer_profile():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth"))

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
    try:
        name = request.form.get('name')
        description = request.form.get('description')
        category = request.form.get('category')
        price = float(request.form.get('price'))
        quantity = int(request.form.get('quantity'))
        status = request.form.get('status') == 'on'
        
        # Handle image
        image_filename = None
        if 'image' in request.files:
            file = request.files['image']
            if file and file.filename:
                image_filename = save_uploaded_image(file)
        
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
    try:
        cake_ref = cakes.document(cake_id)
        cake_doc = cake_ref.get()
        
        if not cake_doc.exists:
            flash('Cake not found!', 'danger')
            return redirect('/admin_panel#cake-availability')
        
        cake_data = cake_doc.to_dict()
        image_filename = cake_data.get('image')
        
        # Delete image
        if image_filename:
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], image_filename)
            if os.path.exists(image_path):
                os.remove(image_path)
        
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

@app.route('/api/send-message', methods=['POST'])
def api_send_message():
    """
    Handle incoming message from chatbot widget
    Saves message to Firestore and returns bot response
    """
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



# ---------------- RUN SERVER ----------------
if __name__ == "__main__":
    app.run(debug=True)