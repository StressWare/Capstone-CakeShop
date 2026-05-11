from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from extensions import limiter
from flask_limiter.errors import RateLimitExceeded
from datetime import datetime, timedelta, timezone
from helpers import (PH_TZ, log_admin_action, convert_timestamps, 
                     calculate_order_total, _today_range, 
                     get_faq_response, save_uploaded_image, delete_uploaded_image, handle_loyalty_stamp)
from decorators import login_required, admin_required, profile_required
from firebase_admin import messaging
import re
import os
import json
import hmac
import hashlib
import secrets
import firebase
from db import sales, expenses, inventory, users, cakes, custom_cake_price, walkin_orders, reviews, admin_logs, orders, notifications, pending_orders, fcm_tokens
from firebase_admin import auth, firestore, messaging
from pyngrok import ngrok
from paymongo import create_checkout_session, verify_payment, build_line_items
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)

app.secret_key = os.getenv('SECRET_KEY')
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024  # 2MB max file size
limiter.init_app(app)
@app.errorhandler(RateLimitExceeded)
def handle_rate_limit(e):
    if request.is_json or request.path.startswith('/verify') or request.path.startswith('/save'):
        return jsonify({"error": "Too many requests. Please slow down."}), 429
    flash("Too many attempts. Please wait a moment.", "danger")
    return redirect(url_for("customer_dashboard")), 429
PAYMONGO_WEBHOOK_SECRET = os.getenv("PAYMONGO_WEBHOOK_SECRET")
@app.after_request
def add_common_headers(response):
    # remove ngrok intro page
    response.headers['ngrok-skip-browser-warning'] = 'true'
    # allow Google auth popups
    response.headers['Cross-Origin-Opener-Policy'] = 'same-origin-allow-popups'
    return response

# BLUEPRINT REGISTRATION
from pos import pos_bp
app.register_blueprint(pos_bp)



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

    # ── Existing: Top rated cakes (untouched) ──
    available_cakes = []
    for cake_doc in cakes.where("status", "==", True).stream():
        cake_data = cake_doc.to_dict()
        cake_data['id']           = cake_doc.id
        cake_data['avg_rating']   = 0
        cake_data['review_count'] = 0
        available_cakes.append(cake_data)

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

    top_cakes = sorted(
        [c for c in available_cakes if c["review_count"] > 0],
        key=lambda x: x["avg_rating"],
        reverse=True
    )[:5]

    # ── New: Most ordered cakes ──
    order_counts = {}
    for order_doc in orders.stream():
        order_data = order_doc.to_dict()
        for item in order_data.get("selected_items", []):
            cake_id  = item.get("cake_id")
            quantity = int(item.get("quantity", 1))
            if cake_id:
                order_counts[cake_id] = order_counts.get(cake_id, 0) + quantity

    # Sort by most ordered, take top 5 IDs
    top_ids = sorted(order_counts, key=order_counts.get, reverse=True)[:5]

    # Fetch each cake document
    most_ordered = []
    for cake_id in top_ids:
        cake_doc = cakes.document(cake_id).get()
        if cake_doc.exists:
            cake_data       = cake_doc.to_dict()
            cake_data['id'] = cake_doc.id
            most_ordered.append(cake_data)

    # Pad with None if less than 5
    while len(most_ordered) < 5:
        most_ordered.append(None)

    return render_template("home.html",
        customer     = customer,
        top_cakes    = top_cakes,
        most_ordered = most_ordered
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
@limiter.limit("10 per minute")
def verify_token():
    data = request.get_json()
    id_token = data.get('idToken')

    if not id_token:
        return jsonify({'error': 'No token provided'}), 400

    try:
        decoded_token = auth.verify_id_token(id_token,  clock_skew_seconds=10)
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
        app.logger.warning("Invalid ID token received")
        return jsonify({'error': 'Invalid token'}), 401
    except auth.ExpiredIdTokenError:
        app.logger.warning("Expired ID token received")
        return jsonify({'error': 'Token expired'}), 401
    except Exception:
        app.logger.exception("Unexpected error in verify_token")
        return jsonify({'error': 'Internal server error'}), 500

# ---------------- SAVE USER DETAILS ----------------
@app.route('/save-user-details', methods=['POST'])
@limiter.limit("10 per minute")
def save_user_details():
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'Missing or invalid token'}), 401

    id_token = auth_header.split('Bearer ')[1]

    try:
        decoded_token = auth.verify_id_token(id_token, clock_skew_seconds=10)
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
        },  merge=True)
        return jsonify({'success': True}), 200

    except auth.InvalidIdTokenError:
        app.logger.warning("Invalid ID token in save_user_details")
        return jsonify({'error': 'Invalid token'}), 401
    except auth.ExpiredIdTokenError:
        app.logger.warning("Expired ID token in save_user_details")
        return jsonify({'error': 'Token expired'}), 401
    except Exception:
        app.logger.exception("Unexpected error in save_user_details")
        return jsonify({'error': 'Internal server error'}), 500

# ---------------- COMPLETE PROFILE ----------------
@app.route('/complete-profile', methods=['GET', 'POST'])
@login_required
def complete_profile():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('auth_page'))

    if request.method == 'POST':
        fname    = (request.form.get('fname') or '').strip()
        username = (request.form.get('username') or '').strip()
        number   = (request.form.get('number') or '').strip()
        address  = (request.form.get('address') or '').strip()

        # same limits as /customer/edit
        if not username or len(username) > 50:
            flash('Invalid username length.', 'danger')
            return redirect(url_for('complete_profile'))

        if not fname or len(fname) > 100:
            flash('Invalid full name length.', 'danger')
            return redirect(url_for('complete_profile'))

        users.document(user_id).update({
            "fname":    fname,
            "username": username,
            "number":   number,
            "address":  address
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
@profile_required
@login_required
def customer_dashboard():
    user_id = session.get("user_id")
 
    doc = users.document(user_id).get()
    if not doc.exists:
        return "User not found", 404
    customer = doc.to_dict()
 
    # ── Orders from top-level collection 
    orders_list = []
    for order_doc in orders.where("user_id", "==", user_id).order_by("created_at", direction="DESCENDING").stream():
        order = order_doc.to_dict()
        order["id"] = order_doc.id
        order["notes"] = order.get("notes", "")
        order["reviewed"] = order.get("reviewed", False)

        order["calculated_total"] = calculate_order_total(order)

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

    # ── Loyalty data ──
    loyalty_stamps    = int(customer.get('loyalty_stamps', 0))
    loyalty_unclaimed = customer.get('loyalty_unclaimed', None)

    active_vouchers = []
    now_dt = datetime.now(PH_TZ)
    for v_doc in users.document(user_id).collection("vouchers").stream():
        v = v_doc.to_dict()
        v['id'] = v_doc.id
        expires = v.get('expires_at')
        if expires and hasattr(expires, 'tzinfo') and expires.tzinfo is None:
            expires = expires.replace(tzinfo=PH_TZ)
        if not v.get('used', False) and expires and expires > now_dt:
            v['expires_at_fmt'] = expires.strftime('%b %d, %Y')
            active_vouchers.append(v)

    return render_template("customer_dashboard.html",
        customer        = customer,
        orders          = orders_list,
        user_id         = user_id,
        cart_count      = cart_count,
        favorite_ids    = favorite_ids,
        favorites_list  = favorites_list,
        loyalty_stamps    = loyalty_stamps,
        loyalty_unclaimed = loyalty_unclaimed,
        active_vouchers   = active_vouchers,
    )

# ---------------- FAVORITES TOGGLE ----------------
@app.route("/favorites/toggle", methods=["POST"])
@profile_required
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
 
    except Exception:
        app.logger.exception("[FAVORITES ERROR] Failed to toggle favorite")
        return jsonify({"success": False, "error": "Internal server error"}), 500

# ---------------- CUSTOMER PROFILE EDIT ----------------
@app.route("/customer/edit", methods=["POST"])
@profile_required
@login_required
def edit_customer_profile():
    user_id  = session.get("user_id")

    username = (request.form.get("username") or "").strip()
    number   = (request.form.get("contact") or "").strip()
    address  = (request.form.get("address") or "").strip()
    fname    = (request.form.get("full_name") or "").strip()

    if not username or len(username) > 50:
        flash("Invalid username.", "danger")
        return redirect(url_for("customer_dashboard"))

    if not fname or len(fname) > 100:
        flash("Invalid name.", "danger")
        return redirect(url_for("customer_dashboard"))

    users.document(user_id).update({
        "username": username,
        "number":   number,
        "address":  address,
        "fname":    fname
    })

    flash("Profile updated successfully!", "success")
    return redirect(url_for("customer_dashboard"))

# ---------------- LOYALTY CLAIM ----------------
@app.route("/loyalty/claim", methods=["POST"])
@profile_required
@login_required
def loyalty_claim():
    user_id = session.get("user_id")
    now     = datetime.now(PH_TZ)

    try:
        user_ref  = users.document(user_id)
        user_data = user_ref.get().to_dict() or {}

        unclaimed = user_data.get('loyalty_unclaimed', None)
        if not unclaimed:
            flash("No reward available to claim.", "warning")
            return redirect(url_for("customer_dashboard") + "#loyalty")

        expires_at = now + timedelta(days=180)  # 6 months

        user_ref.collection("vouchers").add({
            "discount":   unclaimed,
            "claimed_at": now,
            "expires_at": expires_at,
            "used":       False,
            "used_at":    None,
        })

        user_ref.update({"loyalty_unclaimed": None})

        flash(f"🎉 Your {unclaimed}% discount voucher has been claimed! Valid for 6 months.", "success")

    except Exception:
        app.logger.exception("[LOYALTY CLAIM] Failed")
        flash("Something went wrong. Please try again.", "danger")

    return redirect(url_for("customer_dashboard") + "#loyalty")

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
@profile_required
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
    
    order_ref = orders.document(order_id)  # Changed from users.document(user_id).collection("orders")
    order_doc = order_ref.get()
    
    if not order_doc.exists:
        flash("Order not found.", "danger")
        return redirect(url_for("customer_dashboard"))
    
    order_data = order_doc.to_dict()
    
    # ← ADDED: Verify order belongs to this user
    if order_data.get("user_id") != user_id:
        flash("Unauthorized.", "danger")
        return redirect(url_for("customer_dashboard"))
    
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
    def safe_rating(field, default=5):
        txt = request.form.get(field)
        try:
            return max(1, min(5, int(txt))) if txt else default
        except ValueError:
            return default

    if order_data.get("order_type") == "custom":
        flavor_rating  = safe_rating("flavor_rating")
        design_rating  = safe_rating("design_rating")
        overall_rating = safe_rating("rating")
        
        review_data["flavor_rating"]  = flavor_rating
        review_data["design_rating"]  = design_rating
        review_data["overall_rating"] = overall_rating
        review_data["rating"]         = overall_rating
        
    else:
        # Premade orders: single rating
        review_data["rating"] = safe_rating("rating")
    
    # Save review
    reviews.add(review_data)
    
    # Mark order as reviewed in top-level orders collection
    order_ref.update({"reviewed": True})  # ← Now updates top-level orders
    
    flash("Review submitted! Thank you 🎂", "success")
    return redirect(url_for("customer_dashboard"))
# ---------------- RECIEPT PAGE ----------------
@app.route("/order/receipt/<order_id>")
@profile_required
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
    
    order["calculated_total"] = calculate_order_total(order)
    
    return render_template("customer_receipt.html", order=order)

# ================================================================
# ORDER ROUTES
# ================================================================

# ---------------- CUSTOMIZATION ORDER ----------------
@app.route("/order", methods=["POST"])
@limiter.limit("5 per minute")
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
    customer     = customer_doc.to_dict() if customer_doc.exists else {}
    min_date     = (datetime.now(PH_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")

    # ── Recompute amount from Firestore ──
    try:
        icing_key   = request.form.get("design", "").split("|")[0]
        size_key    = request.form.get("cakeSize", "").split("|")[0]
        layers_key  = request.form.get("layers", "").split("|")[0]
        toppers_key = request.form.get("toppers", "").split("|")[0]

        icing_prices   = custom_cake_price.document("icing").get().to_dict()   or {}
        size_prices    = custom_cake_price.document("size").get().to_dict()     or {}
        layers_prices  = custom_cake_price.document("layers").get().to_dict()   or {}
        toppers_prices = custom_cake_price.document("toppers").get().to_dict()  or {}
        addon_prices   = custom_cake_price.document("addons").get().to_dict()   or {}

        amount  = 0.0
        amount += float(icing_prices.get(icing_key, 0))
        amount += float(size_prices.get(size_key, 0))
        amount += float(layers_prices.get(layers_key, 0))
        amount += float(toppers_prices.get(toppers_key, 0))

        for addon_key in ["filling", "cupcake", "ediblepaper", "fondanttoppers"]:
            if request.form.get(addon_key):
                amount += float(addon_prices.get(addon_key, 0))

    except Exception as e:
        app.logger.exception("Error computing custom cake price")
        flash("Unable to compute order price. Please try again.", "danger")
        return redirect(url_for('customize'))
    active_vouchers = []
    now_dt = datetime.now(PH_TZ)
    for v_doc in users.document(user_id).collection("vouchers").stream():
        v = v_doc.to_dict()
        v['id'] = v_doc.id
        expires = v.get('expires_at')
        if expires and hasattr(expires, 'tzinfo') and expires.tzinfo is None:
            expires = expires.replace(tzinfo=PH_TZ)
        if not v.get('used', False) and expires and expires > now_dt:
            v['expires_at_fmt'] = expires.strftime('%b %d, %Y')
            active_vouchers.append(v)
            
    return render_template('checkout.html',
        order_type     = 'custom',
        order_item     = request.form.get('order_item'),
        amount         = amount,          # ← server computed
        notes          = request.form.get('notes', ''),
        rush           = request.form.get('rush', ''),
        inspo_image    = inspo_image,
        selected_items = [],
        customer       = customer,
        min_date       = min_date,
        active_vouchers = active_vouchers,
    )

# ---------------- PREMADE ORDER ----------------
@app.route("/order/cake", methods=["POST"])
@profile_required
@login_required
@limiter.limit("5 per minute")
def order_cake():
    user_id = session.get("user_id")

    customer_doc = users.document(user_id).get()
    customer     = customer_doc.to_dict() if customer_doc.exists else {}
    min_date     = (datetime.now(PH_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")

    selected_json  = request.form.get('selected_items', '[]')
    selected_items = json.loads(selected_json)

    if selected_items:
        for i in selected_items:
            cake_id  = i.get('cake_id')
            quantity = int(i.get('quantity', 1))

            cake_doc = cakes.document(cake_id).get()
            if not cake_doc.exists:
                flash("Cake not found.", "danger")
                return redirect(url_for("customer_dashboard"))

            cake_data  = cake_doc.to_dict()
            real_price = float(cake_data.get('price', 0))
            max_qty    = int(cake_data.get('quantity', 0))  # ← stock
            quantity   = min(quantity, max_qty)             # ← cap

            if quantity < 1:
                flash(f"{cake_data.get('name', 'A cake')} is out of stock.", "danger")
                return redirect(url_for("customer_dashboard"))

            i['quantity']  = quantity
            i['price']     = real_price
            i['subtotal']  = real_price * quantity
            i['cake_name'] = cake_data.get('name', i.get('cake_name', ''))
            i['image_url'] = cake_data.get('image', i.get('image_url', None))
            i['category']  = cake_data.get('category', '')
            i['max_qty']   = max_qty                        # ← for frontend cap

        amount = sum(i['subtotal'] for i in selected_items)

    else:
        cake_id  = request.form.get('cake_id')
        quantity = int(request.form.get('quantity', 1))

        cake_doc = cakes.document(cake_id).get()
        if not cake_doc.exists:
            flash("Cake not found.", "danger")
            return redirect(url_for("customer_dashboard"))

        cake_data  = cake_doc.to_dict()
        real_price = float(cake_data.get('price', 0))
        max_qty    = int(cake_data.get('quantity', 0))      # stock
        quantity   = min(quantity, max_qty)                 # cap

        if quantity < 1:
            flash(f"{cake_data.get('name', 'A cake')} is out of stock.", "danger")
            return redirect(url_for("customer_dashboard"))

        selected_items = [{
            'cake_id':   cake_id,
            'cake_name': cake_data.get('name', ''),
            'price':     real_price,
            'quantity':  quantity,
            'subtotal':  real_price * quantity,
            'image_url': cake_data.get('image', None),
            'category':  cake_data.get('category', ''),
            'max_qty':   max_qty                            
        }]
        amount = real_price * quantity

    active_vouchers = []
    now_dt = datetime.now(PH_TZ)
    for v_doc in users.document(user_id).collection("vouchers").stream():
        v = v_doc.to_dict()
        v['id'] = v_doc.id
        expires = v.get('expires_at')
        if expires and hasattr(expires, 'tzinfo') and expires.tzinfo is None:
            expires = expires.replace(tzinfo=PH_TZ)
        if not v.get('used', False) and expires and expires > now_dt:
            v['expires_at_fmt'] = expires.strftime('%b %d, %Y')
            active_vouchers.append(v)

    return render_template('checkout.html',
        order_type      = 'premade',
        selected_items  = selected_items,
        amount          = amount,
        customer        = customer,
        min_date        = min_date,
        active_vouchers = active_vouchers,
    )
 
# ---------------- PLACE ORDER (FINALIZE OF BOTH PREMADE  OR  CUSTOM) ----------------
@app.route("/place-order", methods=["POST"])
@profile_required
@login_required
@limiter.limit("5 per minute")
def finalize_order():
    user_id = session.get("user_id")
    now     = datetime.now(PH_TZ)

    try:
        date_str = request.form["delivery_date"]
        time_str = request.form["delivery_time"]
        delivery_datetime = datetime.strptime(
            f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=PH_TZ)
    except (KeyError, ValueError):
        flash("Invalid delivery date or time.", "danger")
        return redirect(url_for("customer_dashboard"))

    if delivery_datetime < datetime.now(PH_TZ):
        flash("Delivery date must be in the future.", "danger")
        return redirect(url_for("customer_dashboard"))

    delivery_type  = request.form.get("delivery_type", "Delivery")
    address        = "Pick Up at Shop" if delivery_type == "Pickup" else request.form.get("address", "")
    order_type     = request.form.get("order_type")
    selected_json  = request.form.get("selected_items", "[]")
    payment_method = request.form.get("payment_method", "Cash on Delivery")

    # ── Voucher validation ──
    voucher_id           = request.form.get('voucher_id', '').strip()
    voucher_discount_pct = 0
    if voucher_id:
        now_dt = datetime.now(PH_TZ)
        v_ref  = users.document(user_id).collection("vouchers").document(voucher_id)
        v_doc  = v_ref.get()
        if v_doc.exists:
            v_data  = v_doc.to_dict()
            expires = v_data.get('expires_at')
            if expires and hasattr(expires, 'tzinfo') and expires.tzinfo is None:
                expires = expires.replace(tzinfo=PH_TZ)
            if not v_data.get('used', False) and expires and expires > now_dt:
                voucher_discount_pct = int(v_data.get('discount', 0))

    custom_components = []

    if order_type == "premade":
        selected_items = json.loads(selected_json)
        amount         = 0.0
        normalized     = []

        for i in selected_items:
            cake_id  = i.get("cake_id")
            quantity = int(i.get("quantity", 1))

            cake_doc = cakes.document(cake_id).get()
            if not cake_doc.exists:
                flash("One or more cakes no longer exist.", "danger")
                return redirect(url_for("customer_dashboard"))

            cake_data  = cake_doc.to_dict()
            real_price = float(cake_data.get("price", 0))
            stock      = int(cake_data.get("quantity", 0))  # ← check stock

            if quantity < 1 or quantity > stock:            # ← reject bad qty
                flash(f"{cake_data.get('name', 'A cake')} only has {stock} available.", "danger")
                return redirect(url_for("customer_dashboard"))

            subtotal   = real_price * quantity
            amount    += subtotal

            normalized.append({
                **i,
                "price":     real_price,
                "subtotal":  subtotal,
                "cake_name": cake_data.get("name", i.get("cake_name", "")),
            })

        selected_items = normalized
        item_names     = ", ".join([f"{i['cake_name']} (₱{i['price']:.0f})" for i in selected_items])
        rush           = False
        inspo_image    = None

        for i in selected_items:
            users.document(user_id).collection("cart").document(i["cake_id"]).delete()

    else:
        selected_items = []
        item_names     = request.form.get("order_item", "")
        rush           = request.form.get("rush") == "yes"
        inspo_image    = request.form.get("inspo_image") or None

        try:
            icing_key   = request.form.get("design", "").split("|")[0]
            size_key    = request.form.get("cakeSize", "").split("|")[0]
            layers_key  = request.form.get("layers", "").split("|")[0]
            toppers_key = request.form.get("toppers", "").split("|")[0]

            icing_prices   = custom_cake_price.document("icing").get().to_dict()   or {}
            size_prices    = custom_cake_price.document("size").get().to_dict()     or {}
            layers_prices  = custom_cake_price.document("layers").get().to_dict()   or {}
            toppers_prices = custom_cake_price.document("toppers").get().to_dict()  or {}
            addon_prices   = custom_cake_price.document("addons").get().to_dict()   or {}

            amount  = 0.0
            amount += float(icing_prices.get(icing_key, 0))
            amount += float(size_prices.get(size_key, 0))
            amount += float(layers_prices.get(layers_key, 0))
            amount += float(toppers_prices.get(toppers_key, 0))

            for addon_key in ["filling", "cupcake", "ediblepaper", "fondanttoppers"]:
                if request.form.get(addon_key):
                    amount += float(addon_prices.get(addon_key, 0))

        except Exception:
            app.logger.exception("Error recomputing custom price in place-order")
            flash("Unable to verify order price. Please try again.", "danger")
            return redirect(url_for('customize'))

        item_parts = item_names.split(", ")
        for part in item_parts:
            part = part.strip()
            if not part:
                continue
            if match := re.search(r'^(.+?) \(₱([\d,]+)\)$', part):
                component_name  = match[1].strip()
                component_price = float(match[2].replace(',', ''))
                custom_components.append({"name": component_name, "price": component_price})
            elif match2 := re.search(r'^(.+?) \(([\d,]+)\)$', part):
                component_name  = match2[1].strip()
                component_price = float(match2[2].replace(',', ''))
                custom_components.append({"name": component_name, "price": component_price})

    # ── Apply voucher discount ──
    if voucher_discount_pct > 0:
        if order_type == 'premade':
            cake_subtotal = 0.0
            for item in selected_items:
                cake_doc = cakes.document(item.get('cake_id', '')).get()
                if cake_doc.exists:
                    category = cake_doc.to_dict().get('category', '')
                    if category == 'Cake':
                        cake_subtotal += item.get('subtotal', 0.0)
            discount_amount = round(cake_subtotal * voucher_discount_pct / 100, 2)
        else:
            discount_amount = round(amount * voucher_discount_pct / 100, 2)
        amount = round(amount - discount_amount, 2)
    else:
        discount_amount = 0

    # ── Base order data ──
    order_data = {
        "user_id":        user_id,
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
        "delivery_token": secrets.token_urlsafe(16),
        "voucher_id":       voucher_id if voucher_discount_pct > 0 else None,
        "voucher_discount": voucher_discount_pct if voucher_discount_pct > 0 else None,
        "discount_amount":  discount_amount if voucher_discount_pct > 0 else None,
        "customer": {
            "name":      request.form.get("customer_name", ""),
            "contact":   request.form.get("contact", ""),
            "address":   address,
            "occasion":  request.form.get("occasion", ""),
            "celebrant": request.form.get("celebrant", ""),
            "age":       request.form.get("age", ""),
            "lat":       float(request.form.get("lat")) if request.form.get("lat") else None,
            "lng":       float(request.form.get("lng")) if request.form.get("lng") else None,
        },
        "created_at": now
    }

    # ── Deduct stock for premade orders ──
    if order_type == "premade":
        for item in selected_items:
            cake_id          = item.get("cake_id")
            quantity_ordered = int(item.get("quantity", 1))
            cake_ref         = cakes.document(cake_id)
            cake_doc         = cake_ref.get()

            if not cake_doc.exists:
                flash("One or more cakes no longer exist.", "danger")
                return redirect(url_for("customer_dashboard"))

            current_qty = cake_doc.to_dict().get("quantity", 0)

            if quantity_ordered > current_qty:          # ← last line of defense
                name = cake_doc.to_dict().get("name", "A cake")
                flash(f"{name} only has {current_qty} available.", "danger")
                return redirect(url_for("customer_dashboard"))

            new_qty = current_qty - quantity_ordered
            cake_ref.update({"quantity": new_qty, "status": new_qty > 0})

    # ── COD or Bank Transfer → save immediately ──
    if payment_method in ["Cash on Delivery", "Bank Transfer"]:
        orders.add(order_data)
        handle_loyalty_stamp(users, user_id, order_type, selected_items, cakes)
        if voucher_id and voucher_discount_pct > 0:
            users.document(user_id).collection("vouchers").document(voucher_id).update({
                "used":    True,
                "used_at": datetime.now(PH_TZ)
            })
        flash("Order placed successfully! 🎂", "success")
        return redirect(url_for("customer_dashboard"))

    # ── Online Payment → PayMongo ──
    line_items = build_line_items(order_type, selected_items, amount)

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

    pending_orders.document(checkout["session_id"]).set({
        "user_id": user_id,
        "order_data": {
            **order_data,
            "delivery_date": delivery_datetime.isoformat(),
            "created_at":    now.isoformat()
        }
    })
    session['paymongo_session_id'] = checkout["session_id"]
    return redirect(checkout["checkout_url"])
# ---------------- CUSTOMER CANCEL ORDER ----------------
@app.route("/order/cancel/<order_id>", methods=["POST"])
@profile_required
@login_required
@limiter.limit("10 per minute")
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
# DELIVERY ROUTES
# ================================================================
# ---------------- RIDER DELIVERY PAGE ----------------
@app.route("/delivery/<token>")
@limiter.limit("30 per minute")
def delivery_page(token):
    # Find order by delivery_token
    results = orders.where("delivery_token", "==", token).limit(1).stream()
    order_doc = next(results, None)
 
    if not order_doc:
        return render_template("delivery.html", expired=True)
 
    order = order_doc.to_dict()
    order["id"] = order_doc.id
 
    # Convert timestamps
    order = convert_timestamps(order)
 
    # If order is completed, show expired page
    if order.get("status") == "Completed":
        return render_template("delivery.html", expired=True)
 
    return render_template("delivery.html", order=order, expired=False)
# ---------------- NOTIFY DELIVERY TO ADMIN ----------------
@app.route("/delivery/<token>/notify", methods=["POST"])
@limiter.limit("5 per minute")
def notify_delivery(token):
    try:
        results = orders.where("delivery_token", "==", token).limit(1).stream()
        order_doc = next(results, None)

        if not order_doc:
            return {"error": "Order not found"}, 404

        order = order_doc.to_dict()

        if order.get("status") == "Completed":
            return {"error": "Order already completed"}, 400

        admin_tokens_doc = fcm_tokens.document("admins").get()

        if not admin_tokens_doc.exists:
            app.logger.warning("[FCM] No admin tokens doc found")
            return {"error": "No admin tokens found"}, 500

        token_map = admin_tokens_doc.to_dict()  # {uid: token}
        tokens = list(token_map.values())

        if not tokens:
            return {"error": "No admin tokens registered"}, 500

        order_id = order_doc.id
        customer_name = order.get("customer", {}).get("name", "Customer")

        # 5. Send one message per token
        success_count = 0
        failed_uids = []

        for uid, tok in token_map.items():
            try:
                msg = messaging.Message(
                    token=tok,
                    notification=messaging.Notification(
                        title="🛵 Order Delivered!",
                        body=f"{customer_name}'s order has been delivered. Tap to mark as completed."
                    ),
                    data={"order_id": order_id, "type": "delivery_complete"},
                    webpush=messaging.WebpushConfig(
                        notification=messaging.WebpushNotification(
                            icon="/static/img/logo.png",
                            badge="/static/img/logo.png",
                        )
                    )
                )
                messaging.send(msg)
                success_count += 1
                app.logger.info(f"[FCM] Sent to uid: {uid}")
            except Exception as send_err:
                app.logger.warning(f"[FCM] Failed for uid {uid}: {send_err}")
                failed_uids.append(uid)

        if failed_uids:
            admin_ref = fcm_tokens.document("admins")
            updates = {uid: firestore.DELETE_FIELD for uid in failed_uids}
            admin_ref.update(updates)
            app.logger.info(f"[FCM] Removed {len(failed_uids)} invalid token(s)")

        app.logger.info(f"[FCM] Done: {success_count}/{len(tokens)} succeeded")
        return {"success": True, "notified": success_count}, 200

    except Exception as e:
        app.logger.exception(f"[FCM] Notify error: {e}")
        return {"error": str(e)}, 500

# ================================================================
# CAKES ROUTES
# ================================================================
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

    # Fetch reviews with Firestore ordering 
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
        favorite_ids.extend(
            doc.id
            for doc in users.document(user_id).collection("favorites").stream()
        )
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
@profile_required
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
@profile_required
@login_required
def add_to_cart():
    user_id   = session.get("user_id")
    cake_id   = request.form.get("cake_id")
    cake_name = request.form.get("cake_name")
    quantity  = int(request.form.get("quantity", 1))
    image     = request.form.get("image")

    # ── Fetch real price from Firestore ──
    cake_doc = cakes.document(cake_id).get()
    if not cake_doc.exists:
        flash("Cake not found.", "danger")
        return redirect(url_for("cakes_page"))

    cake_data  = cake_doc.to_dict()
    real_price = float(cake_data.get("price", 0))
    image      = image or cake_data.get("image")

    cart_ref = users.document(user_id).collection("cart").document(cake_id)
    cart_doc = cart_ref.get()

    if cart_doc.exists:
        existing_qty = cart_doc.to_dict().get("quantity", 1)
        new_qty      = existing_qty + quantity
        cart_ref.update({"quantity": new_qty})
        flash(f"{cake_data.get('name', cake_name)} quantity updated to {new_qty} in cart! 🛒", "success")
    else:
        cart_ref.set({
            "cake_id":   cake_id,
            "cake_name": cake_data.get("name", cake_name),
            "price":     real_price,
            "quantity":  quantity,
            "image":     image,
            "added_at":  firestore.SERVER_TIMESTAMP
        })
        flash(f"{cake_data.get('name', cake_name)} added to cart! 🛒", "success")

    return redirect(url_for("cakes_page"))
# ---------------- REMOVE FROM CART ----------------
@app.route("/cart/remove/<cake_id>", methods=["POST"])
@profile_required
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
    # ---- Low Stock ----
    low_stock = [doc.to_dict() for doc in inventory.where("quantity", "<", 10).stream()]

    # ---- All Orders ----
    all_orders = []
    for order_doc in orders.order_by("created_at", direction="DESCENDING").limit(200).stream():
        order = order_doc.to_dict()
        order["id"] = order_doc.id
        order = convert_timestamps(order)
        all_orders.append(order)

    # ---- Status Counters ----
    total_new = total_accepted = total_pending = total_ready = 0
    total_out = total_completed = total_cancelled = total_rush = 0

    # ---- Today's Deliveries ----
    today_date = datetime.now(PH_TZ).date()
    today_start = datetime.combine(today_date, datetime.min.time()).replace(tzinfo=PH_TZ)
    today_end   = datetime.combine(today_date, datetime.max.time()).replace(tzinfo=PH_TZ)
    today_count = 0
    today_deliveries = []

    # ---- Daily Report: Online Premade & Custom ----
    pre_sales = pre_txn = pre_cash = pre_ewallet = 0
    cus_sales = cus_txn = cus_cash = cus_ewallet = 0
    pre_items = {}

    def is_today(ts):
        return isinstance(ts, datetime) and today_start <= ts <= today_end

    def classify_payment_online(method):
        return "cash" if method and "cash" in method.lower() else "ewallet"

    def classify_payment_walkin(method):
        return "cash" if method and method.lower() == "cash" else "ewallet"

    for order in all_orders:
        status = order.get("status", "")

        # Status counters
        if status == "New":                total_new += 1
        elif status == "Accepted":         total_accepted += 1
        elif status == "Pending":          total_pending += 1
        elif status == "Ready":            total_ready += 1
        elif status == "Out for Delivery": total_out += 1
        elif status == "Completed":        total_completed += 1
        elif status == "Cancelled":        total_cancelled += 1
        if order.get("rush"):              total_rush += 1

        # Today's deliveries
        delivery_date = order.get("delivery_date")
        if isinstance(delivery_date, datetime) and delivery_date.date() == today_date and status not in ["Completed", "Cancelled"]:
            today_count += 1
            today_deliveries.append({
                    "time":     delivery_date.strftime("%I:%M %p"),
                    "customer": order.get("customer", {}).get("name", "N/A"),
                    "cake":     order.get("item", "N/A"),
                    "status":   status,
                    "rush":     order.get("rush", False)
                })

        # Daily report — online orders (filter by created_at today)
        ts = order.get("created_at")
        if not is_today(ts):
            continue

        otype = order.get("order_type", "")
        amt   = order.get("amount", 0) or 0
        pm    = classify_payment_online(order.get("payment_method"))

        if otype == "premade":
            pre_sales += amt
            pre_txn   += 1
            if pm == "cash": pre_cash += amt
            else:            pre_ewallet += amt
            for item in order.get("selected_items", []):
                name = item.get("cake_name", "")
                if name:
                    pre_items[name] = pre_items.get(name, 0) + 1

        elif otype == "custom":
            cus_sales += amt
            cus_txn   += 1
            if pm == "cash": cus_cash += amt
            else:            cus_ewallet += amt

    pre_top = max(pre_items, key=pre_items.get) if pre_items else "—"

    today_deliveries.sort(key=lambda x: datetime.strptime(x["time"], "%I:%M %p"))

    # ---- Daily Report: POS / Walk-in ----
    pos_sales = pos_txn = pos_cash = pos_ewallet = 0
    pos_items = {}

    try:
        start_ts, end_ts = _today_range()
        walkin_stream = walkin_orders.where("created_at", ">=", start_ts).where(
            "created_at", "<", end_ts
        ).stream()
    except Exception:
        walkin_stream = walkin_orders.stream()

    for doc in walkin_stream:
        w = doc.to_dict()
        w = convert_timestamps(w)
        ts = w.get("created_at")
        if not is_today(ts):
            continue
        amt = w.get("amount", 0) or 0
        pos_sales += amt
        pos_txn   += 1
        pm = classify_payment_walkin(w.get("payment_method"))
        if pm == "cash": pos_cash += amt
        else:            pos_ewallet += amt
        for item in w.get("order_items", []):
            name = item.get("cake_name", "")
            if name:
                pos_items[name] = pos_items.get(name, 0) + 1

    pos_top = max(pos_items, key=pos_items.get) if pos_items else "—"

    return render_template("admin_dashboard.html",
        # Status overview
        low_stock=low_stock,
        total_new=total_new,
        total_accepted=total_accepted,
        total_pending=total_pending,
        total_ready=total_ready,
        total_out=total_out,
        total_completed=total_completed,
        total_cancelled=total_cancelled,
        total_rush=total_rush,
        # Today's deliveries
        today_count=today_count,
        today_deliveries=today_deliveries,
        # Daily report — POS
        pos_sales=pos_sales,
        pos_txn=pos_txn,
        pos_top=pos_top,
        pos_cash=pos_cash,
        pos_ewallet=pos_ewallet,
        # Daily report — Online Premade
        pre_sales=pre_sales,
        pre_txn=pre_txn,
        pre_top=pre_top,
        pre_cash=pre_cash,
        pre_ewallet=pre_ewallet,
        # Daily report — Online Custom
        cus_sales=cus_sales,
        cus_txn=cus_txn,
        cus_cash=cus_cash,
        cus_ewallet=cus_ewallet,
    )
@app.route("/admin/calendar-orders")
@admin_required
def calendar_orders():
    """
    Returns orders whose delivery_date matches the requested date or month.
    Excludes Cancelled and Completed orders.
    Query param: ?date=YYYY-MM-DD  → returns order list for that day
    Query param: ?month=YYYY-MM    → returns badge counts + dot info per day
    """
    date_str  = request.args.get("date")
    month_str = request.args.get("month")
 
    # ── Per-day detail ──────────────────────────────────────────────
    if date_str:
        try:
            target = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400
 
        day_start = datetime.combine(target, datetime.min.time()).replace(tzinfo=PH_TZ)
        day_end   = datetime.combine(target, datetime.max.time()).replace(tzinfo=PH_TZ)
 
        try:
            day_docs = (
                orders
                .where("delivery_date", ">=", day_start)
                .where("delivery_date", "<=", day_end)
                .where("status", "not-in", ["Cancelled", "Completed"])
                .stream()
            )
        except Exception as e:
            return jsonify({"error": f"Firestore query failed: {str(e)}"}), 500
 
        result = []
        for order_doc in day_docs:
            order = order_doc.to_dict()
            order = convert_timestamps(order)
            delivery_date = order.get("delivery_date")
 
            if not isinstance(delivery_date, datetime):
                continue
 
            result.append({
                "id":             order_doc.id,
                "customer":       order.get("customer", {}).get("name", "N/A"),
                "item":           order.get("item", "N/A"),
                "status":         order.get("status", ""),
                "rush":           order.get("rush", False),
                "order_type":     order.get("order_type", ""),
                "payment_method": order.get("payment_method", "—"),
                "time":           delivery_date.strftime("%I:%M %p"),
            })
 
        result.sort(key=lambda x: datetime.strptime(x["time"], "%I:%M %p"))
        return jsonify({"orders": result})
 
    # ── Month overview ───────────────────────────────────────────────
    elif month_str:
        try:
            month_start = datetime.strptime(month_str + "-01", "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "Invalid month format. Use YYYY-MM"}), 400
 
        if month_start.month == 12:
            month_end = month_start.replace(year=month_start.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            month_end = month_start.replace(month=month_start.month + 1, day=1) - timedelta(days=1)
 
        range_start = datetime.combine(month_start, datetime.min.time()).replace(tzinfo=PH_TZ)
        range_end   = datetime.combine(month_end,   datetime.max.time()).replace(tzinfo=PH_TZ)
 
        try:
            month_docs = (
                orders
                .where("delivery_date", ">=", range_start)
                .where("delivery_date", "<=", range_end)
                .where("status", "not-in", ["Cancelled", "Completed"])
                .stream()
            )
        except Exception as e:
            return jsonify({"error": f"Firestore query failed: {str(e)}"}), 500
 
        # Per day: { "YYYY-MM-DD": { premade: n, custom: n, rush: bool } }
        days = {}
        for order_doc in month_docs:
            order = order_doc.to_dict()
            order = convert_timestamps(order)
            delivery_date = order.get("delivery_date")
 
            if not isinstance(delivery_date, datetime):
                continue
 
            key        = delivery_date.strftime("%Y-%m-%d")
            order_type = order.get("order_type", "")
            is_rush    = order.get("rush", False)
 
            if key not in days:
                days[key] = {"premade": 0, "custom": 0, "rush": False}
 
            if order_type == "premade":
                days[key]["premade"] += 1
            elif order_type == "custom":
                days[key]["custom"] += 1
 
            if is_rush:
                days[key]["rush"] = True
 
        return jsonify({"days": days})
 
    return jsonify({"error": "Provide ?date=YYYY-MM-DD or ?month=YYYY-MM"}), 400
# ---------------- ADMIN ORDERS ----------------
@app.route("/admin/orders")
@admin_required
def admin_orders():
    # No more Firestore fetching here!
    # onSnapshot in the frontend handles everything
    return render_template("admin_orders.html")
# ---------------- ADMIN DELIVERY ----------------
@app.route("/admin/delivery")
@admin_required
def admin_delivery():
    # Get all Out for Delivery orders
    active_deliveries = []
 
    delivery_docs = orders.where("status", "==", "Out for Delivery").where("delivery_type", "==", "Delivery").stream()
 
    for doc in delivery_docs:
        order = doc.to_dict()
        order["id"] = doc.id
        order = convert_timestamps(order)
        active_deliveries.append(order)
 
    # Sort by delivery_date
    active_deliveries.sort(
        key=lambda x: x.get("delivery_date") or datetime.min.replace(tzinfo=PH_TZ)
    )
 
    return render_template("admin_delivery.html", orders=active_deliveries)
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
    online_sales = []
    walkin_sales = []

    # Online orders (completed)
    for doc in orders.where("status", "==", "Completed").order_by("created_at", direction="DESCENDING").stream():
        order = doc.to_dict()
        order["id"] = doc.id

        # Convert created_at to datetime if it's a string
        created_at = order.get("created_at")
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            except Exception:
                created_at = datetime.now(PH_TZ)
        elif created_at is None:
            created_at = datetime.now(PH_TZ)

        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
            else:
                created_at = created_at.astimezone(PH_TZ)
        order["created_at"] = created_at

        online_sales.append(order)

    # Walk-in orders (completed)
    for doc in walkin_orders.where("status", "==", "Completed").order_by("created_at", direction="DESCENDING").stream():
        order = doc.to_dict()
        order["id"] = doc.id

        # Convert created_at to datetime if it's a string
        created_at = order.get("created_at")
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            except Exception:
                created_at = datetime.now(PH_TZ)
        elif created_at is None:
            created_at = datetime.now(PH_TZ)

        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
            else:
                created_at = created_at.astimezone(PH_TZ)

        order["created_at"] = created_at


        walkin_sales.append(order)

    return render_template("admin_sales.html", 
        online_sales=online_sales, 
        walkin_sales=walkin_sales,
        online_count=len(online_sales),
        walkin_count=len(walkin_sales)
    )
    

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

    # ── Monthly (this year) ──
    monthly_sales    = {m: 0 for m in months}
    monthly_expenses = {m: 0 for m in months}

    # ── All time (by year-month) ──
    alltime_data = {}  # "Jan 2025" → {sales, expenses, profit}

    # ── Summary cards ──
    total_revenue   = 0
    total_expenses  = 0
    total_orders    = 0
    total_completed = 0
    payment_counts  = {}
    premade_sales   = {}
    custom_sales    = {}

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
        total_expenses += cost

        if isinstance(date_val, datetime):
            # Weekly
            if date_val >= week_ago:
                weekly_expenses[date_val.strftime("%a")] += cost

            # Monthly
            if date_val.year == now.year:
                monthly_expenses[date_val.strftime("%b")] += cost

            # All time
            key = date_val.strftime("%b %Y")
            if key not in alltime_data:
                alltime_data[key] = {"sales": 0, "expenses": 0, "profit": 0, "sort": date_val}
            alltime_data[key]["expenses"] += cost

    # ── Fetch orders ──
    for order_doc in orders.stream():
        order = order_doc.to_dict()
        status = order.get("status", "")
        amount = float(order.get("amount", 0))

        total_orders += 1
        if status == "Completed":
            total_completed += 1

        payment = order.get("payment_method", "Unknown")
        payment_counts[payment] = payment_counts.get(payment, 0) + 1

        order_type = order.get("order_type", "")

        # Track premade cake sales
        if order_type == "premade":
            for item in order.get("selected_items", []):
                name = item.get("cake_name", "Unknown")
                premade_sales[name] = premade_sales.get(name, 0) + 1


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

    # ── Compute profits — floor at 0 per period to avoid negative display ──
    weekly_profit = {}
    for day in days_order:
        raw = weekly_sales[day] - weekly_expenses[day]
        weekly_profit[day] = max(0, raw)

    monthly_profit = {}
    for m in months:
        raw = monthly_sales[m] - monthly_expenses[m]
        monthly_profit[m] = max(0, raw)

    for key in alltime_data:
        raw = alltime_data[key]["sales"] - alltime_data[key]["expenses"]
        alltime_data[key]["profit"] = max(0, raw)

    alltime_sorted       = sorted(alltime_data.items(), key=lambda x: x[1]["sort"])
    alltime_labels       = [k for k, v in alltime_sorted]
    alltime_sales_vals   = [v["sales"]    for k, v in alltime_sorted]
    alltime_expense_vals = [v["expenses"] for k, v in alltime_sorted]
    alltime_profit_vals  = [v["profit"]   for k, v in alltime_sorted]

    # Top 3 premade cakes
    top_premade       = sorted(premade_sales.items(), key=lambda x: x[1], reverse=True)[:3]
    top_premade_names  = [c[0] for c in top_premade]
    top_premade_counts = [c[1] for c in top_premade]

    # Net profit (all time)
    net_profit = total_revenue - total_expenses

    # Stats
    completion_rate = round((total_completed / total_orders * 100), 1) if total_orders > 0 else 0
    avg_order_value = round(total_revenue / total_completed, 2)        if total_completed > 0 else 0

    return render_template("admin_analytics.html",
        now              = now,
        # Weekly
        weekly_sales     = weekly_sales,
        weekly_expenses  = weekly_expenses,
        weekly_profit    = weekly_profit,
        days_order       = days_order,
        # Monthly
        monthly_sales    = monthly_sales,
        monthly_expenses = monthly_expenses,
        monthly_profit   = monthly_profit,
        months           = months,
        # All time
        alltime_labels       = alltime_labels,
        alltime_sales_vals   = alltime_sales_vals,
        alltime_expense_vals = alltime_expense_vals,
        alltime_profit_vals  = alltime_profit_vals,
        # Summary cards
        total_revenue    = total_revenue,
        total_expenses   = total_expenses,
        net_profit       = net_profit,
        total_orders     = total_orders,
        completion_rate  = completion_rate,
        avg_order_value  = avg_order_value,
        # Charts
        top_premade_names  = top_premade_names,
        top_premade_counts = top_premade_counts,
        payment_counts     = payment_counts,
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

    # Get all order counts 
    order_counts = {}
    for order_doc in orders.stream():  
        if uid := order_doc.to_dict().get("user_id"):
            order_counts[uid] = order_counts.get(uid, 0) + 1

    users_ref = users.order_by("created_at", direction="DESCENDING").stream()
    for user_doc in users_ref:
        user_data = user_doc.to_dict()
        user_data['uid'] = user_doc.id
        user_data['order_count'] = order_counts.get(user_doc.id, 0)  

        try:
            auth_user = auth.get_user(user_doc.id)
            user_data['disabled'] = auth_user.disabled
            user_data['email_verified'] = auth_user.email_verified
            user_data['created_at'] = datetime.fromtimestamp(auth_user.user_metadata.creation_timestamp / 1000, tz=PH_TZ)
        except Exception:
            user_data['disabled'] = False
            user_data['email_verified'] = False
            user_data['created_at'] = None
        all_users.append(user_data)

    return render_template("admin_users.html", all_users=all_users)

# ---------------- ADMIN REVIEWS PAGE----------------
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
# ---------------- ADMIN LOGS PAGE----------------
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
@app.route("/order/status/<order_id>", methods=["POST"])
@admin_required
def update_order_status(order_id):
    new_status = request.form["status"]
    order_ref = orders.document(order_id)
    order_doc = order_ref.get()

    if order_doc.exists:
        order_data = order_doc.to_dict()
        old_status = order_data.get("status")
        order_type = order_data.get("order_type", "custom")

        # Quantity deduction logic 
        if new_status == "Accepted" and old_status == "New" and order_type == "premade":
            for i in order_data.get("selected_items", []):
                cake_ref = cakes.document(i["cake_id"])
                cake_doc = cake_ref.get()
                if cake_doc.exists:
                    current_qty = cake_doc.to_dict().get("quantity", 0)
                    quantity_ordered = int(i.get("quantity", 1))
                    new_qty = max(0, current_qty - quantity_ordered)
                    cake_ref.update({"quantity": new_qty, "status": new_qty > 0})

        accepted_statuses = ["Accepted", "Pending", "Ready", "Out for Delivery"]
        if new_status == "Cancelled" and old_status in accepted_statuses and order_type == "premade":
            for i in order_data.get("selected_items", []):
                cake_ref = cakes.document(i["cake_id"])
                cake_doc = cake_ref.get()
                if cake_doc.exists:
                    current_qty = cake_doc.to_dict().get("quantity", 0)
                    restore_qty = int(i.get("quantity", 1))
                    cake_ref.update({"quantity": current_qty + restore_qty, "status": True})

        # Update order status
        order_ref.update({"status": new_status})
        
        # CREATE NOTIFICATION
        status_messages = {
            "Accepted": "has been accepted",
            "Pending": "is now being prepared",
            "Ready": "is ready for pickup/delivery",
            "Out for Delivery": "is out for delivery",
            "Completed": "has been completed",
            "Cancelled": "has been cancelled"
        }
        
        message = status_messages.get(new_status, f"is now {new_status}")
        notify_user_id = order_data.get("user_id")
        notifications.add({
            "user_id": notify_user_id,
            "order_id": order_id,
            "title": f"Order {new_status}",
            "message": f"Your order #{order_id[:8]} {message}",
            "type": "status_update",
            "is_read": False,
            "created_at": datetime.now(PH_TZ)
        })
        print(f"Creating notification for user {notify_user_id}, order {order_id}, status {new_status}")
        log_admin_action(
            action=f"Changed order status to '{new_status}'",
            target=f"Order #{order_id} — {order_data.get('customer', {}).get('name', 'Customer')}",
            category="order"
        )
    return jsonify({"success": True, "message": f"Order status updated to {new_status}"})

# ---------------- EDIT ORDER ----------------
@app.route("/order/edit/<order_id>", methods=["POST"])
@admin_required
def edit_order( order_id):
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
        return jsonify({"success": True, "message": "Order updated successfully!"})
    except Exception:
        app.logger.exception("Error updating order")
        return jsonify({"success": False, "message": "Failed to update order. Please try again."}), 500


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
    try:
        new_cost = float(request.form["cost"].replace(",", ""))
    except ValueError:
        flash("Invalid cost value. Please enter a number.", "danger")
        return redirect(url_for("admin_expenses"))
    
    expenses.document(id).update({
        "cost": new_cost
    })
    log_admin_action(
        action="Edited expenses item",
        target=request.form["cost"],
        category="expense"
    )
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
        return jsonify({"success": True, "message": "Cake added successfully!"})
    except Exception:
        app.logger.exception("Error adding cake")
        return jsonify({"success": False, "message": "Failed to add cake."}), 500

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
        return jsonify({"success": True, "message": "Cake updated successfully!"})
    except Exception:
        app.logger.exception(f"Error editing cake {cake_id}")
        return jsonify({"success": False, "message": "Failed to update cake."}), 500

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
        delete_uploaded_image(image_url)  # ← replaces the 6 lines

        cake_name = cake_doc.to_dict().get('name', cake_id)
        cake_ref.delete()
        log_admin_action(
            action="Deleted cake",
            target=f"{cake_name} (ID: {cake_id})",
            category="cake"
        )
        return jsonify({"success": True, "message": "Cake deleted successfully!"})
    except Exception:
        app.logger.exception(f"Error deleting cake {cake_id}")
        return jsonify({"success": False, "message": "Failed to delete cake."}), 500

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
# PAYMENT ROUTES
# ================================================================
# ---------------- PAYMENT WEBHOOK ----------------
@app.route("/paymongo/webhook", methods=["POST"])
def paymongo_webhook():
    raw_body = request.get_data()
    signature_header = request.headers.get("Paymongo-Signature", "")

    # Verify signature
    if PAYMONGO_WEBHOOK_SECRET:
        try:
            parts = dict(p.split("=", 1) for p in signature_header.split(","))
            timestamp = parts.get("t", "")
            # use "te" for test mode, "li" for live mode
            received_sig = parts.get("te") or parts.get("li", "")

            # Build the string to sign: timestamp + "." + raw_body
            signed_payload = f"{timestamp}.{raw_body.decode('utf-8')}"

            expected = hmac.new(
                PAYMONGO_WEBHOOK_SECRET.encode(),
                signed_payload.encode(),
                hashlib.sha256
            ).hexdigest()

            if not hmac.compare_digest(expected, received_sig):
                app.logger.warning("Invalid PayMongo webhook signature")
                return jsonify({"status": "invalid signature"}), 400

        except Exception as e:
            app.logger.warning(f"Webhook signature verification error: {e}")
            return jsonify({"status": "signature error"}), 400
    payload = request.get_json(force=True)

    event_type = payload.get("data", {}).get("attributes", {}).get("type")
    data = payload.get("data", {}).get("attributes", {}).get("data", {})

    if event_type == "checkout_session.payment.paid":
        session_id = data.get("id")
        attributes = data.get("attributes", {})

        # Get pending order from Firestore
        pending_ref = pending_orders.document(session_id)
        pending_doc = pending_ref.get()

        if not pending_doc.exists:
            return jsonify({"status": "order not found"}), 404

        pending = pending_doc.to_dict()
        order_data = pending["order_data"]

        # Get payment details
        payments = attributes.get("payments", [])
        payment_method = "Unknown"
        payment_id = None
        if payments:
            payment_method = payments[0].get("attributes", {}).get("source", {}).get("type", "Unknown").upper()
            payment_id = payments[0].get("id")

        # Update order data
        order_data["delivery_date"] = datetime.fromisoformat(order_data["delivery_date"])
        order_data["created_at"] = datetime.fromisoformat(order_data["created_at"])
        order_data["payment_status"] = "Paid"
        order_data["payment_id"] = payment_id
        order_data["payment_method"] = payment_method
        order_data["paymongo_session_id"] = session_id 
        # Save to orders collection
        orders.add(order_data)

        # Delete pending order
        pending_ref.delete()
    #no balance/insufficienet fund/card declined, etc
    elif event_type == "payment.failed":
        if session_id := data.get("attributes", {}).get("checkout_session_id"):
            pending_orders.document(session_id).delete()
            app.logger.info(f"Deleted pending order due to failed payment: {session_id}")
    return jsonify({"status": "ok"}), 200
# ---------------- PAYMENT SUCCESS ----------------
@app.route("/payment/success")
@login_required
def payment_success():
    session_id = session.get('paymongo_session_id')

    if not session_id:
        flash("Invalid payment session.", "danger")
        return redirect(url_for("customer_dashboard"))

    # Check if webhook already processed it
    completed = orders.where("paymongo_session_id", "==", session_id).limit(1).stream()
    if order_doc := next(completed, None):
        # Webhook already handled it
        saved_order = order_doc.to_dict()
        saved_order["id"] = order_doc.id
        session.pop('paymongo_session_id', None)
        return render_template("payment_success.html", order=saved_order, payment_result={"paid": True})

    # Webhook hasn't fired yet, fall back to polling
    payment_result = verify_payment(session_id)

    if not payment_result.get("paid"):
        flash("Payment not confirmed. Please try again.", "danger")
        session.pop('paymongo_session_id', None)
        return redirect(url_for("customer_dashboard"))

    # Get pending order from Firestore
    pending_ref = pending_orders.document(session_id)
    pending_doc = pending_ref.get()

    if not pending_doc.exists:
        flash("Order data not found. Please contact support.", "danger")
        session.pop('paymongo_session_id', None)
        return redirect(url_for("customer_dashboard"))

    pending = pending_doc.to_dict()
    order_data = pending["order_data"]

    order_data["delivery_date"] = datetime.fromisoformat(order_data["delivery_date"])
    order_data["created_at"]    = datetime.fromisoformat(order_data["created_at"])
    order_data["payment_status"] = "Paid"
    order_data["payment_id"]     = payment_result.get("reference")
    order_data["payment_method"] = payment_result.get("payment_method", order_data["payment_method"]).upper()
    order_data["paymongo_session_id"] = session_id

    # Save to orders
    doc_ref = orders.add(order_data)
    order_id = doc_ref[1].id

    handle_loyalty_stamp(
        users,
        order_data.get('user_id'),
        order_data.get('order_type'),
        order_data.get('selected_items', []),
        cakes
    )
        # ── Mark voucher as used ── ← ADD
    v_id = order_data.get('voucher_id', '')
    if v_id:
        users.document(order_data['user_id']).collection("vouchers").document(v_id).update({
            "used":    True,
            "used_at": datetime.now(PH_TZ)
        })
        
    # Delete pending order
    pending_ref.delete()

    session.pop('paymongo_session_id', None)

    saved_order = order_data.copy()
    saved_order["id"] = order_id

    return render_template("payment_success.html",
        order=saved_order,
        payment_result=payment_result
    )
 

# ---------------- PAYMENT FAILED ----------------
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
@login_required
@limiter.limit("20 per minute")
def send_message():
    try:
        data = request.get_json()
        user_id = session['user_id']
        message = data.get('message', '').strip()
        conversation_id = data.get('conversation_id')
        is_escalation = data.get('is_escalation', False)

        if not message or not conversation_id:
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
            bot_response = (
                "✅ Thank you! The shop owner has been notified and will respond shortly. You're now chatting with the owner."
                if is_escalation
                else get_faq_response(message)
            )
            # Save bot response
            messages_ref.add({
                "text": bot_response,
                "sender": "bot",
                "timestamp": now,
                "created_at": now
            })
        elif is_escalation:
            # Special message for escalation
            messages_ref.add({
                "text": "✅ You're now connected with the shop owner. They'll respond shortly.",
                "sender": "bot",
                "timestamp": now,
                "created_at": now
            })

        return jsonify({'success': True, 'escalated': is_escalated})

    except Exception:
        app.logger.exception("Error in send_message")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500
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
        
    except Exception:
        app.logger.exception("Error in admin_reply")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500


# ---------------- GET CONVERSATION STATUS ----------------
@app.route('/conversation-status/<user_id>/<conversation_id>', methods=['GET'])
@login_required
def get_conversation_status(user_id, conversation_id):
    try:
        current_user = session.get('user', {})
        if user_id != session.get('user_id') and not current_user.get('admin'):
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403

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
        
    except Exception:
        app.logger.exception("Error in get_conversation_status")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500

@app.route('/admin/delete-conversation', methods=['POST'])
@admin_required
def delete_conversation():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        conversation_id = data.get('conversation_id')
        
        if not user_id or not conversation_id:
            return jsonify({'success': False, 'error': 'Missing data'}), 400
        
        # Delete the conversation document
        conv_ref = users.document(user_id).collection("conversations").document(conversation_id)
        conv_ref.delete()
        
        # Optional: Delete all messages in subcollection
        messages = conv_ref.collection("messages").stream()
        for msg in messages:
            msg.reference.delete()
        
        return jsonify({'success': True})
    except Exception as e:
        app.logger.exception("Error deleting conversation")
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
                        "last_time_dt": ts or None,  # 👈 add this
                        "escalated": convo_data.get('escalated', False)  # Now convo_data exists
                    })
                    
                except Exception:
                    app.logger.exception(f"Error processing conversation {convo_doc.id}")
                    continue
        
        all_convos.sort(key=lambda x: x["last_time_dt"] or datetime.min.replace(tzinfo=PH_TZ), reverse=True)
        
        return render_template("admin_conversations.html", conversations=all_convos)
        
    except Exception:
        app.logger.exception("Error in admin_conversations")
        flash("Error loading conversations", "danger")
        return render_template("admin_conversations.html", conversations=[])
    
#----------------- PWA(PROGRESSIVE WEB APP) ----------------
@app.route('/service-worker.js')
def service_worker():
    return app.send_static_file('javascript/service-worker.js')

@app.route('/manifest.json')
def manifest():
    return app.send_static_file('manifest.json')

@app.route('/manifest-admin.json')
def manifest_admin():
    return app.send_static_file('manifest-admin.json')
 
@app.route('/manifest-delivery.json')
def manifest_delivery():
    return app.send_static_file('manifest-delivery.json')
 
@app.route('/service-worker-admin.js')
def service_worker_admin():
    return app.send_static_file('javascript/service-worker-admin.js')
 
@app.route('/service-worker-delivery.js')
def service_worker_delivery():
    return app.send_static_file('javascript/service-worker-delivery.js')
@app.route('/manifest-pos.json')
def manifest_pos():
    return app.send_static_file('manifest-pos.json')

@app.route('/service-worker-pos.js')
def service_worker_pos():
    return app.send_static_file('javascript/service-worker-pos.js')


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