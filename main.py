from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_wtf.csrf import CSRFProtect
from flask_talisman import Talisman
from extensions import limiter, send_order_confirmation
from flask_limiter.errors import RateLimitExceeded
from datetime import datetime, timedelta, timezone
from helpers import (PH_TZ, log_admin_action, convert_timestamps, 
                     calculate_order_total, _today_range, 
                     get_faq_response, save_uploaded_image, delete_uploaded_image, handle_loyalty_stamp,safe_float)
from decorators import login_required, admin_required, profile_required
from utils import get_all_cakes, get_all_reviews, get_order_counts,get_custom_prices,get_locked_dates_cached,invalidate_cache
from firebase_admin import messaging
import requests as http_requests
import re
import os
import json
import hmac
import hashlib
import secrets
import firebase
from db import db, sales, expenses, inventory, users, cakes, custom_cake_price, walkin_orders, reviews, admin_logs, orders, notifications, pending_orders, fcm_tokens, conversations,locked_dates_ref
from firebase_admin import auth, firestore, messaging
from pyngrok import ngrok
from paymongo import create_checkout_session, verify_payment, build_line_items
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY')
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024  # 2MB max file size
is_production = os.environ.get('FLASK_ENV') == 'production'
app.config['SESSION_COOKIE_SECURE'] = is_production   
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)  
PAYMONGO_WEBHOOK_SECRET = os.getenv("PAYMONGO_WEBHOOK_SECRET")

csrf = CSRFProtect(app)
Talisman(app,
    force_https=is_production,
    session_cookie_secure=is_production,
    session_cookie_http_only=True,
    session_cookie_samesite='Lax',
    content_security_policy=False
)

limiter.init_app(app)
@app.errorhandler(RateLimitExceeded)
def handle_rate_limit(e):
    if request.is_json or request.path.startswith('/verify') or request.path.startswith('/save'):
        return jsonify({"error": "Too many requests. Please slow down."}), 429
    flash("Too many attempts. Please wait a moment.", "danger")
    return redirect(url_for("customer_dashboard")), 429

@app.after_request
def add_common_headers(response):
    # remove ngrok intro page
    response.headers['ngrok-skip-browser-warning'] = 'true'
    # allow Google auth popups
    response.headers['Cross-Origin-Opener-Policy'] = 'same-origin-allow-popups'
    return response

# POS BLUEPRINT REGISTRATION
from pos import pos_bp
app.register_blueprint(pos_bp)

#ERROR HANDLER
@app.errorhandler(403)
def forbidden(e):
    return render_template('403.html'), 403

@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(500)
def server_error(e):
    return render_template('500.html'), 500

@app.errorhandler(405)
def method_not_allowed(e):
    return render_template('405.html'), 405

# ================================================================
# PUBLIC ROUTES
# ================================================================
# ---------------- HOME PAGE ----------------
@app.route("/")
def home_page():
    # ❌ Not cached — user specific
    user_id  = session.get("user_id")
    customer = None
    if user_id:
        doc = users.document(user_id).get()
        if doc.exists:
            customer = doc.to_dict()

    # ✅ All heavy reads from cache
    available_cakes = get_all_cakes()
    all_reviews     = get_all_reviews()
    order_counts    = get_order_counts()

    # ── Top rated cakes ──
    for cake in available_cakes:
        cake["avg_rating"]   = 0
        cake["review_count"] = 0

    cake_ratings = {}
    for r in all_reviews:
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

    # ── Most ordered ──
    top_ids      = sorted(order_counts, key=order_counts.get, reverse=True)[:5]
    cakes_by_id  = {c["id"]: c for c in available_cakes}
    most_ordered = [cakes_by_id.get(cid) for cid in top_ids]

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
@app.route('/authentication')
def auth_page():
    return render_template('authentication.html',
        recaptcha_site_key=os.environ.get('RECAPTCHA_SITE_KEY')
    )
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
    if not data:
        return jsonify({'error': 'Invalid request'}), 400
    id_token = data.get('idToken')
    recaptcha_token = data.get('recaptchaToken')
     # ── reCAPTCHA check ──
    if recaptcha_token:
        try:
            r = http_requests.post(
                'https://www.google.com/recaptcha/api/siteverify',
                data={
                    'secret':   os.environ.get('RECAPTCHA_SECRET_KEY'),
                    'response': recaptcha_token
                },
                timeout=5
            )
            result = r.json()
            score = result.get('score', 0)
            print(f"DEBUG reCAPTCHA score: {score}, success: {result.get('success')}")
            if not result.get('success') or score < 0.5:
                app.logger.warning(f"reCAPTCHA failed: score={score}")
                return jsonify({'error': 'Suspicious activity detected.'}), 403
        except Exception:
            app.logger.warning("reCAPTCHA check failed, proceeding anyway")

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

        if not user_doc.exists and is_google:
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
        
        username = (data.get('username') or '').strip()
        fname    = (data.get('fname') or '').strip()
        number   = (data.get('number') or '').strip()
        address  = (data.get('address') or '').strip()
        
        if not username or len(username) > 50:
            return jsonify({'error': 'Invalid username'}), 400
        if not fname or len(fname) > 100:
            return jsonify({'error': 'Invalid full name'}), 400
        if not number or not re.match(r'^[0-9+\-\s]{7,15}$', number):
            return jsonify({'error': 'Invalid phone number'}), 400
        if not address or len(address) > 255:
            return jsonify({'error': 'Invalid address'}), 400
        users.document(uid).set({
            'username': username, 
            'number':   number,
            'address':  address,
            'fname':    fname,
            'email':    decoded_token.get('email', ''),
            'role':     'customer',
            'created_at': firestore.SERVER_TIMESTAMP
        }, merge=True)
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
@limiter.limit("5 per minute")
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
        if not number or not re.match(r'^[0-9+\-\s]{7,15}$', number):
            flash('Invalid phone number.', 'danger')
            return redirect(url_for('complete_profile'))

        if not address or len(address) > 255:
            flash('Invalid address.', 'danger')
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
        if not isinstance(expires, datetime):
            continue
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=PH_TZ)
        if not v.get('used', False) and expires > now_dt:
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
@limiter.limit("30 per minute")
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
@limiter.limit("5 per minute")
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
    
    if not number or not re.match(r'^[0-9+\-\s]{7,15}$', number):
        flash("Invalid phone number.", "danger")
        return redirect(url_for("customer_dashboard"))

    if not address or len(address) > 255:
        flash("Invalid address.", "danger")
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
@limiter.limit("3 per minute")
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

@app.route("/customize_cake")
def customize():
    customer = session.get("user_id")
    custom_prices = get_custom_prices()
    return render_template("customization.html", customer=customer, custom_prices=custom_prices)

# ---------------- ADD REVIEW PAGE ----------------
@app.route("/review/add", methods=["POST"])
@profile_required
@login_required
@limiter.limit("5 per minute")
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
    invalidate_cache("all_reviews")
    # Mark order as reviewed in top-level orders collection
    order_ref.update({"reviewed": True})  # ← Now updates top-level orders
    
    flash("Review submitted! Thank you 🎂", "success")
    return redirect(url_for("customer_dashboard"))
# ---------------- RECEIPT PAGE ----------------
@app.route("/order/receipt/<order_id>")
@profile_required
@login_required
def order_receipt(order_id):
    user_id = session.get("user_id")

    order_ref = orders.document(order_id)
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

    # ── FIXED: use OR so any valid payment state grants access ──
    status  = order.get("status")
    payment = order.get("payment_status")

    if status != "Completed" and payment not in ("Paid", "Downpayment Paid"):
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
     #Shop lock check 
    today = datetime.now(PH_TZ).strftime("%Y-%m-%d")
    info  = get_locked_dates_cached().get(today)
    if info and info.get('lock_custom'):
        flash("Custom cake orders are unavailable today.", "danger")
        return redirect(url_for("customize"))
    
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

        prices         = get_custom_prices()
        icing_prices   = prices["icing"]
        size_prices    = prices["size"]
        layers_prices  = prices["layers"]
        toppers_prices = prices["toppers"]
        addon_prices   = prices["addons"]

        amount  = 0.0
        amount += float(icing_prices.get(icing_key, 0))
        amount += float(size_prices.get(size_key, 0))
        amount += float(layers_prices.get(layers_key, 0))
        amount += float(toppers_prices.get(toppers_key, 0))

        for addon_key in ["filling", "cupcake", "ediblepaper", "fondanttoppers", "sprinkles", "drip", "flowers"]:
            if request.form.get(addon_key):
                amount += float(addon_prices.get(addon_key, 0))
                

    except Exception as e:
        print(f"DEBUG custom amount after recompute: {amount}")
        print(f"DEBUG icing_key: {icing_key}, size_key: {size_key}, layers_key: {layers_key}, toppers_key: {toppers_key}")
        print(f"DEBUG prices — icing: {icing_prices}, size: {size_prices}")
        app.logger.exception("Error computing custom cake price")
        flash("Unable to compute order price. Please try again.", "danger")
        return redirect(url_for('customize'))
    active_vouchers = []
    now_dt = datetime.now(PH_TZ)
    for v_doc in users.document(user_id).collection("vouchers").stream():
        v = v_doc.to_dict()
        v['id'] = v_doc.id
        expires = v.get('expires_at')
        if not isinstance(expires, datetime):
            continue
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=PH_TZ)
        if not v.get('used', False) and expires > now_dt:
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
        cake_design         = request.form.get('design', '0|0'),
        cake_size           = request.form.get('cakeSize', '6|0'),
        cake_layers         = request.form.get('layers', '1|0'),
        cake_toppers        = request.form.get('toppers', '0|0'),
        cake_filling        = request.form.get('filling', ''),
        cake_cupcake        = request.form.get('cupcake', ''),
        cake_ediblepaper    = request.form.get('ediblepaper', ''),
        cake_fondanttoppers = request.form.get('fondanttoppers', ''),
        cake_sprinkles      = request.form.get('sprinkles', ''),
        cake_drip           = request.form.get('drip', ''),
        cake_flowers        = request.form.get('flowers', ''),
    )

# ---------------- PREMADE ORDER ----------------
@app.route("/order/cake", methods=["POST"])
@profile_required
@login_required
@limiter.limit("5 per minute")
def order_cake():
    user_id = session.get("user_id")
    # Shop lock check
    today = datetime.now(PH_TZ).strftime("%Y-%m-%d")
    info  = get_locked_dates_cached().get(today)
    if info and info.get('lock_premade'):
        flash("Orders are unavailable today.", "danger")
        return redirect(url_for("cakes_page"))
    customer_doc = users.document(user_id).get()
    customer     = customer_doc.to_dict() if customer_doc.exists else {}
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
        if not isinstance(expires, datetime):
            continue
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=PH_TZ)
        if not v.get('used', False) and expires > now_dt:
            v['expires_at_fmt'] = expires.strftime('%b %d, %Y')
            active_vouchers.append(v)

    return render_template('checkout.html',
        order_type      = 'premade',
        selected_items  = selected_items,
        amount          = amount,
        customer        = customer,
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

    order_type    = request.form.get("order_type")
    delivery_type = request.form.get("delivery_type", "Delivery")
    address       = "Pick Up at Shop" if delivery_type == "Pickup" else request.form.get("address", "")
    if order_type not in ("premade", "custom"):
        flash("Invalid order type.", "danger")
        return redirect(url_for("customer_dashboard"))

    if delivery_type not in ("Delivery", "Pickup"):
        delivery_type = "Delivery"

    ALLOWED_PAYMENTS = {"Cash on Delivery", "Online Payment"}
    payment_method = request.form.get("payment_method", "Cash on Delivery")
    if payment_method not in ALLOWED_PAYMENTS:
        flash("Invalid payment method.", "danger")
        return redirect(url_for("customer_dashboard"))
    if order_type == "custom" and payment_method != "Online Payment":
        flash("Custom cake orders require online payment.", "danger")
        return redirect(url_for("customize"))
    selected_json = request.form.get("selected_items", "[]")
    try:
        parsed = json.loads(selected_json)
        if not isinstance(parsed, list):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        flash("Invalid order data.", "danger")
        return redirect(url_for("customer_dashboard"))

    inspo_image_raw = request.form.get("inspo_image", "").strip()
    if inspo_image_raw and not inspo_image_raw.startswith("https://res.cloudinary.com/"):
        flash("Invalid image reference.", "danger")
        return redirect(url_for("customer_dashboard"))

    if delivery_type == "Delivery" and len(address) > 300:
        flash("Address too long. Max 300 characters.", "danger")
        return redirect(url_for("customer_dashboard"))
    
    # Shop lock check
    today = datetime.now(PH_TZ).strftime("%Y-%m-%d")
    info  = get_locked_dates_cached().get(today)
    if info:
        if order_type == 'premade' and info.get('lock_premade'):
            flash("Orders are unavailable today.", "danger")
            return redirect(url_for("customer_dashboard"))
        if order_type == 'custom' and info.get('lock_custom'):
            flash("Orders are unavailable today.", "danger")
            return redirect(url_for("customer_dashboard"))
    if order_type == "premade":
        delivery_datetime = datetime.now(PH_TZ)
    else:
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
    # ── Idempotency check ──
    idempotency_key = request.form.get("idempotency_key", "").strip()
    if idempotency_key:
        existing = orders.where("idempotency_key", "==", idempotency_key)\
                        .where("user_id", "==", user_id)\
                        .limit(1).stream()
        if next(existing, None):
            flash("Order already placed.", "warning")
            return redirect(url_for("customer_dashboard"))
        
    downpayment_type   = None
    downpayment_amount = None
    remaining_balance  = None
    if order_type == "custom":
        raw_dp_type = request.form.get("downpayment_type", "full").strip()
        if raw_dp_type not in ("50", "75", "full"):
            raw_dp_type = "full"
        downpayment_type = raw_dp_type
    #  Voucher validation 
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
    #  Customer info validation 
    customer_name = request.form.get("customer_name", "").strip()
    contact       = request.form.get("contact", "").strip()
    occasion      = request.form.get("occasion", "").strip()
    celebrant     = request.form.get("celebrant", "").strip()
    age           = request.form.get("age", "").strip()
    notes         = request.form.get("notes", "").strip()
    delivery_instructions = request.form.get("delivery_instructions", "").strip()
    if not customer_name or len(customer_name) > 100:
        flash("Invalid name.", "danger")
        return redirect(url_for("customer_dashboard"))

    if not contact or not re.match(r'^[0-9+\-\s]{7,15}$', contact):
        flash("Invalid contact number.", "danger")
        return redirect(url_for("customer_dashboard"))

    if len(notes) > 500:
        flash("Notes too long. Max 500 characters.", "danger")
        return redirect(url_for("customer_dashboard"))
    delivery_instructions = request.form.get("delivery_instructions", "").strip()
    if len(delivery_instructions) > 300:
        flash("Delivery instructions too long. Max 300 characters.", "danger")
        return redirect(url_for("customer_dashboard"))
    if len(occasion) > 100:
        flash("Occasion too long.", "danger")
        return redirect(url_for("customer_dashboard"))

    if len(celebrant) > 100:
        flash("Celebrant name too long.", "danger")
        return redirect(url_for("customer_dashboard"))

    if age and not age.isdigit():
        flash("Invalid age.", "danger")
        return redirect(url_for("customer_dashboard"))
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
                "category":  cake_data.get("category", ""),
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

            prices         = get_custom_prices()
            icing_prices   = prices["icing"]
            size_prices    = prices["size"]
            layers_prices  = prices["layers"]
            toppers_prices = prices["toppers"]
            addon_prices   = prices["addons"]

            amount  = 0.0
            print(f"DEBUG keys — icing:{icing_key} size:{size_key} layers:{layers_key} toppers:{toppers_key}")
            print(f"DEBUG prices — size:{size_prices}")
            amount += float(icing_prices.get(icing_key, 0))
            amount += float(size_prices.get(size_key, 0))
            amount += float(layers_prices.get(layers_key, 0))
            amount += float(toppers_prices.get(toppers_key, 0))

            for addon_key in ["filling", "cupcake", "ediblepaper", "fondanttoppers", "sprinkles", "drip", "flowers"]:
                if request.form.get(addon_key):
                    amount += float(addon_prices.get(addon_key, 0))

        except Exception as e:
            app.logger.exception("Error recomputing custom price in place-order")
            print(f"DEBUG recompute error: {e}")
            import traceback
            traceback.print_exc()
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
    #  Rush fee (custom only) 
    RUSH_FEE = 300.0
    if rush:
        amount = round(amount + RUSH_FEE, 2)
    #  Add delivery fee 
    DELIVERY_FEE = 50.0
    if delivery_type == "Delivery":
        amount = round(amount + DELIVERY_FEE, 2)
    # Apply voucher discount 
    if voucher_discount_pct > 0:
        if order_type == 'premade':
            cake_subtotal = 0.0
            for item in selected_items:
                if item.get('category') == 'Cake':
                    cake_subtotal += float(item.get('subtotal', 0.0))
            discount_amount = round(cake_subtotal * voucher_discount_pct / 100, 2)
        else:
            # discount on cake amount only, not delivery fee
            cake_amount = amount - (DELIVERY_FEE if delivery_type == "Delivery" else 0.0) - (RUSH_FEE if rush else 0.0)
            discount_amount = round(cake_amount * voucher_discount_pct / 100, 2)
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
        "notes":    notes,
        "delivery_instructions": delivery_instructions,
        "payment_method": payment_method,
        "payment_status": "Pending",
        "payment_id":     None,
        "delivery_type":  delivery_type,
        "rush_fee": RUSH_FEE if rush else 0.0,
        "delivery_fee": DELIVERY_FEE if delivery_type == "Delivery" else 0.0,
        "inspo_image":    inspo_image,
        "order_type":     order_type,
        "custom_components": custom_components,
        "delivery_token": secrets.token_urlsafe(16),
        "idempotency_key": idempotency_key,
        "voucher_id":       voucher_id if voucher_discount_pct > 0 else None,
        "voucher_discount": voucher_discount_pct if voucher_discount_pct > 0 else None,
        "discount_amount":  discount_amount if voucher_discount_pct > 0 else None,
        "downpayment_type":   downpayment_type,
        "downpayment_amount": None,  # filled below after amount is final
        "remaining_balance":  None,
        "customer": {
            "name":      customer_name,
            "contact":   contact,
            "address":   address,
            "occasion":  occasion,
            "celebrant": celebrant,
            "age":       age,
            "lat":       safe_float(request.form.get("lat")),
            "lng":       safe_float(request.form.get("lng")),
        },
        "created_at": now
    }
    # ── Compute final downpayment/balance amounts for custom orders ──
    if order_type == "custom" and downpayment_type:
        if downpayment_type == "50":
            dp_amt  = round(amount * 0.50, 2)
            bal_amt = round(amount - dp_amt, 2)
        elif downpayment_type == "75":
            dp_amt  = round(amount * 0.75, 2)
            bal_amt = round(amount - dp_amt, 2)
        else:  # full
            dp_amt  = amount
            bal_amt = 0.0

        order_data["downpayment_amount"] = dp_amt
        order_data["remaining_balance"]  = bal_amt

        # payment_status reflects whether fully or partially paid
        if downpayment_type == "full":
            order_data["payment_status"] = "Pending"  # will be set to Paid on PayMongo success
        else:
            order_data["payment_status"] = "Pending"  # will be set to Downpayment Paid on success

        # The actual amount charged to PayMongo = downpayment amount
        charge_amount = dp_amt
    else:
        charge_amount = amount
    # ── Deduct stock for premade orders (transactional fb built in decorator) ──
    if order_type == "premade"and payment_method in ["Cash on Delivery", "Bank Transfer"]:
        @firestore.transactional
        def deduct_stock(transaction, items):
            for item in items:
                cake_id          = item.get("cake_id")
                quantity_ordered = int(item.get("quantity", 1))
                cake_ref         = cakes.document(cake_id)
                cake_snap        = cake_ref.get(transaction=transaction)

                if not cake_snap.exists:
                    raise ValueError(f"NOEXIST:{cake_id}")

                current_qty = cake_snap.to_dict().get("quantity", 0)
                if quantity_ordered > current_qty:
                    name = cake_snap.to_dict().get("name", "A cake")
                    raise ValueError(f"OVERSTOCK:{name}:{current_qty}")

                new_qty = current_qty - quantity_ordered
                transaction.update(cake_ref, {"quantity": new_qty, "status": new_qty > 0})

        try:
            transaction = db.transaction()
            deduct_stock(transaction, selected_items)
        except ValueError as e:
            parts = str(e).split(":", 2)
            if parts[0] == "NOEXIST":
                flash("One or more cakes no longer exist.", "danger")
            else:
                flash(f"{parts[1]} only has {parts[2]} available.", "danger")
            return redirect(url_for("customer_dashboard"))

    # ── COD  save immediately ──
    if payment_method == "Cash on Delivery":
        doc_ref  = orders.add(order_data)
        order_id = doc_ref[1].id
        users.document(user_id).update({"order_count": firestore.Increment(1)})
        invalidate_cache("order_counts", "all_cakes")  # cache reset

        # send confirmation email
        user_doc = users.document(user_id).get()
        fname    = user_doc.to_dict().get("fname", "Customer")
        email    = user_doc.to_dict().get("email", "")
        send_order_confirmation(
            fname=fname,
            email=email,
            order_id=order_id,
            amount=order_data.get("amount", 0),
            payment_method=payment_method,
            rush_fee=order_data.get("rush_fee", 0),
            delivery_fee=order_data.get("delivery_fee", 0),
            discount_amount=order_data.get("discount_amount", 0),
            downpayment_type=order_data.get("downpayment_type"),
            downpayment_amount=order_data.get("downpayment_amount"),
            remaining_balance=order_data.get("remaining_balance"),
        )

        handle_loyalty_stamp(users, user_id, order_type, selected_items, cakes)
        if voucher_id and discount_amount > 0:
            users.document(user_id).collection("vouchers").document(voucher_id).update({
                "used":    True,
                "used_at": datetime.now(PH_TZ)
            })
        return redirect(url_for("cod_success", order_id=order_id))

    # ── Online Payment → PayMongo ──
    line_items = build_line_items(
        order_type         = order_type,
        selected_items     = selected_items,
        amount             = amount,           # full discounted amount (for premade line items)
        downpayment_type   = downpayment_type,
        downpayment_amount = order_data.get("downpayment_amount"),
        remaining_balance  = order_data.get("remaining_balance"),
        discount_amount    = discount_amount,
        delivery_fee       = order_data.get("delivery_fee", 0),
        rush_fee           = order_data.get("rush_fee", 0),
    )

    base_url    = request.host_url.rstrip('/')
    success_url = f"{base_url}/payment/success"
    cancel_url  = f"{base_url}/payment/failed"
    print(f"DEBUG charge_amount: {charge_amount}")
    print(f"DEBUG charge_amount * 100: {int(charge_amount * 100)}")
    print(f"DEBUG downpayment_type: {downpayment_type}")
    print(f"DEBUG downpayment_amount in order_data: {order_data.get('downpayment_amount')}")
    print(f"DEBUG line_items: {line_items}")
    checkout = create_checkout_session(
        amount            = int(charge_amount * 100),  # ← downpayment or full
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

    # Restore stock if premade order
    if order.get("order_type") == "premade":
        for item in order.get("selected_items", []):
            cake_ref = cakes.document(item["cake_id"])
            cake_doc = cake_ref.get()
            if cake_doc.exists:
                current_qty = cake_doc.to_dict().get("quantity", 0)
                restore_qty = int(item.get("quantity", 1))
                cake_ref.update({"quantity": current_qty + restore_qty, "status": True})

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
@csrf.exempt
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

        # ✅ NEW: prevent duplicate notify
        if order.get("notify_sent"):
            return {"error": "Already notified"}, 400

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

        #  NEW: update order status + timestamps before sending FCM
        order_doc.reference.update({
            "status": "Delivered",
            "delivered_at": firestore.SERVER_TIMESTAMP,
            "notify_sent": True
        })

        # Send one message per token
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
    all_cakes   = get_all_cakes()
    all_reviews = get_all_reviews()

    # Filter active cakes in Python
    available_cakes = [c for c in all_cakes if c.get("status") == True]

    # Build ratings and reviews from cache
    cake_ratings = {}
    cake_reviews = {}

    for r in all_reviews:
        cid = r.get("cake_id")
        if not cid:
            continue

        # Ratings
        if cid not in cake_ratings:
            cake_ratings[cid] = {"total": 0, "count": 0}
        cake_ratings[cid]["total"] += r.get("rating", 0)
        cake_ratings[cid]["count"] += 1

        # Reviews
        if cid not in cake_reviews:
            cake_reviews[cid] = []

        created_at = r.get("created_at")
        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
            else:
                created_at = created_at.astimezone(PH_TZ)

        cake_reviews[cid].append({
            "rating":        r.get("rating", 5),
            "comment":       r.get("comment", ""),
            "reviewer_name": r.get("reviewer_name", "Customer"),
            "created_at":    created_at
        })

    # Sort reviews by created_at descending (was done by Firestore before)
    for cid in cake_reviews:
        cake_reviews[cid].sort(key=lambda x: x["created_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    # Attach ratings and reviews to cakes
    for cake in available_cakes:
        cid = cake["id"]
        cake["avg_rating"]   = 0
        cake["review_count"] = 0
        cake["reviews"]      = []

        if cid in cake_ratings:
            data = cake_ratings[cid]
            cake["avg_rating"]   = round(data["total"] / data["count"], 1)
            cake["review_count"] = data["count"]

        cake["reviews"] = cake_reviews.get(cid, [])

    # Favorites
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
@limiter.exempt
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
@limiter.exempt
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
        walkin_count=len(walkin_sales),
        today=datetime.now(PH_TZ).strftime("%B %d, %Y")
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
    cakes_list = get_all_cakes()
    custom_prices = get_custom_prices()
    return render_template("admin_cakes.html", cakes=cakes_list, custom_prices=custom_prices)

# ---------------- ADMIN USERS ----------------
@app.route("/admin/users")
@admin_required
def admin_users():
    all_users = []

    # ── Fetch all auth users in one call ──
    auth_users = {}
    for auth_user in auth.list_users().iterate_all():
        auth_users[auth_user.uid] = auth_user

    users_ref = users.order_by("created_at", direction="DESCENDING").stream()
    for user_doc in users_ref:
        user_data = user_doc.to_dict()
        user_data['uid'] = user_doc.id
        user_data['order_count'] = user_data.get('order_count', 0)

        auth_user = auth_users.get(user_doc.id) 
        if auth_user:
            user_data['disabled'] = auth_user.disabled
            user_data['email_verified'] = auth_user.email_verified
            user_data['created_at'] = datetime.fromtimestamp(
                auth_user.user_metadata.creation_timestamp / 1000, tz=PH_TZ
            )
        else:
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
# ---------------- MARK BALANCE COLLECTED ----------------
@app.route("/order/balance-collected/<order_id>", methods=["POST"])
@admin_required
def mark_balance_collected(order_id):
    """Admin marks the remaining balance as collected from customer."""
    try:
        order_ref = orders.document(order_id)
        order_doc = order_ref.get()

        if not order_doc.exists:
            return jsonify({"success": False, "message": "Order not found."})

        order_data = order_doc.to_dict()

        # Guard: only valid for custom orders with downpayment
        if order_data.get("order_type") != "custom":
            return jsonify({"success": False, "message": "Only custom orders can have balance collected."})

        if order_data.get("payment_status") != "Downpayment Paid":
            return jsonify({"success": False, "message": "Order is not in Downpayment Paid status."})

        order_ref.update({
            "payment_status":  "Fully Paid",
            "remaining_balance": 0,
            "balance_collected_at": datetime.now(PH_TZ)
        })

        log_admin_action(
            action   = "Marked balance as collected",
            target   = f"Order #{order_id} — {order_data.get('customer', {}).get('name', 'Customer')}",
            category = "order"
        )

        # Notify customer
        notify_user_id = order_data.get("user_id")
        if notify_user_id:
            notifications.add({
                "user_id":    notify_user_id,
                "order_id":   order_id,
                "title":      "Payment Complete",
                "message":    f"Your remaining balance for order #{order_id[:8]} has been collected. You're fully paid! 🎂",
                "type":       "payment_update",
                "is_read":    False,
                "created_at": datetime.now(PH_TZ)
            })

        return jsonify({"success": True, "message": "Balance marked as collected. Order is now Fully Paid."})

    except Exception as e:
        app.logger.exception("Error in mark_balance_collected")
        return jsonify({"success": False, "message": str(e)})
# ---------------- CUSTOMER ORDER RESTRICTION----------------
@app.route("/locked-date-today")
@limiter.exempt
def locked_date_today():
    today  = datetime.now(PH_TZ).strftime("%Y-%m-%d")
    dates  = get_locked_dates_cached()
    info   = dates.get(today)
    if info:
        return jsonify({
            "locked":       True,
            "reason":       info.get("reason", "Unavailable"),
            "lock_custom":  info.get("lock_custom", False),
            "lock_premade": info.get("lock_premade", False)
        })
    return jsonify({"locked": False, "lock_custom": False, "lock_premade": False})

# ---------------- CUSTOMER CUSTOM ORDER RESTRICTION----------------
@app.route("/locked-dates")
@limiter.exempt
def get_locked_dates():
    dates = get_locked_dates_cached()
    return jsonify({"locked_dates": dates})

# ---------------- ADMIN LOCK DATE----------------
@app.route("/admin/lock-date", methods=["POST"])
@admin_required
@limiter.exempt
def lock_date():
    data         = request.get_json()
    date         = data.get("date")
    reason       = data.get("reason", "").strip()
    lock_custom  = data.get("lock_custom", False)
    lock_premade = data.get("lock_premade", False)

    if not date or not reason:
        return jsonify({"error": "Date and reason required"}), 400

    if not lock_custom and not lock_premade:
        return jsonify({"error": "Select at least one order type to lock"}), 400

    locked_dates_ref.document(date).set({
        "reason":       reason,
        "lock_custom":  lock_custom,
        "lock_premade": lock_premade,
        "locked_at":    datetime.now(PH_TZ),
        "locked_by":    session.get("admin_id")
    })
    invalidate_cache("locked_dates")
    return jsonify({"success": True})

# ---------------- ADMIN UNLOCK DATE----------------
@app.route("/admin/lock-date/<date>", methods=["DELETE"])
@admin_required
@limiter.exempt
def unlock_date(date):
    locked_dates_ref.document(date).delete()
    invalidate_cache("locked_dates")
    return jsonify({"success": True})

# ---------------- UPDATE ORDER STATUS ----------------
@app.route("/order/status/<order_id>", methods=["POST"])
@admin_required
def update_order_status(order_id):
    try: 
        new_status = request.form["status"]
        order_ref = orders.document(order_id)
        order_doc = order_ref.get()

        if order_doc.exists:
            order_data = order_doc.to_dict()
            old_status = order_data.get("status")
            order_type = order_data.get("order_type", "custom")


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
            update_data = {"status": new_status}
            if new_status == "Completed" and order_data.get("payment_method") == "Cash on Delivery":
                update_data["payment_status"] = "Paid"

            order_ref.update(update_data)
            
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
            print(f"DEBUG user_id value: '{notify_user_id}'")
            notifications.add({
                "user_id": notify_user_id,
                "order_id": order_id,
                "title": f"Order {new_status}",
                "message": f"Your order #{order_id[:8]} {message}",
                "type": "status_update",
                "is_read": False,
                "created_at": datetime.now(PH_TZ)
            })
            print("DEBUG notification added successfully")
            print(f"Creating notification for user {notify_user_id}, order {order_id}, status {new_status}")
            log_admin_action(
                action=f"Changed order status to '{new_status}'",
                target=f"Order #{order_id} — {order_data.get('customer', {}).get('name', 'Customer')}",
                category="order"
            )
    
        return jsonify({"success": True, "message": f"Order status updated to {new_status}"})
    except Exception as e:
        print(f"ERROR in update_order_status: {e}")            # ← catch silent crashes
        return jsonify({"success": False, "message": str(e)})

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
    category = request.form["category"]  # Ingredients or Equipment
    now      = datetime.now(PH_TZ)
 
    inventory.add({
        "item": item,
        "quantity": quantity,
        "cost": cost,
        "category": category,
        "created_at": now,
        "updated_at": now
    })
 
    # Auto-log to expenses using form category
    expenses.add({
        "description": f"Purchased {quantity} x {item}",
        "cost": cost * quantity,
        "date": now,
        "category": category
    })
 
    log_admin_action(
        action="Added inventory item",
        target=f"{item} — qty: {quantity}, cost: ₱{cost}, category: {category}",
        category="inventory"
    )
    flash('Inventory item added!', 'success')
    return redirect(url_for("admin_inventory"))

# ---------------- EDIT INVENTORY ----------------
@app.route("/inventory/edit/<id>", methods=["POST"])
@admin_required
def edit_inventory(id):
    now = datetime.now(PH_TZ)
 
    inventory.document(id).update({
        "item":     request.form["item"],
        "quantity": int(request.form["quantity"]),
        "cost":     float(request.form["cost"]),
        "category": request.form.get("category", "Ingredients"),
        "updated_at": now
    })
 
    log_admin_action(
        action="Edited inventory item",
        target=request.form["item"],
        category="inventory"
    )
    flash('Inventory item updated!', 'success')
    return redirect(url_for("admin_inventory"))

# ---------------- DELETE INVENTORY ----------------
@app.route("/inventory/delete/<id>", methods=["POST"])
@admin_required
def delete_inventory(id):
    doc  = inventory.document(id).get()
    name = doc.to_dict().get("item", id) if doc.exists else id
    inventory.document(id).delete()
    log_admin_action(
        action="Deleted inventory item",
        target=name,
        category="inventory"
    )
    flash(f'"{name}" deleted from inventory.', 'success')
    return redirect(url_for("admin_inventory"))
 
 
# ---------------- RESTOCK INVENTORY ----------------
@app.route("/inventory/restock/<id>", methods=["POST"])
@admin_required
def restock_inventory(id):
    add_qty  = int(request.form["quantity"])
    cost     = float(request.form["cost"])
    now      = datetime.now(PH_TZ)
 
    doc      = inventory.document(id).get()
    data     = doc.to_dict()
    old_qty  = data.get("quantity", 0)
    item     = data.get("item", "Unknown")
    category = data.get("category", "Ingredients")
 
    # Add to existing quantity
    inventory.document(id).update({
        "quantity":   old_qty + add_qty,
        "updated_at": now
    })
 
    # Auto-log restock to expenses
    expenses.add({
        "description": f"Restocked {add_qty} x {item}",
        "cost": cost * add_qty,
        "date": now,
        "category": category
    })
 
    log_admin_action(
        action="Restocked inventory item",
        target=f"{item} — added qty: {add_qty}, cost: ₱{cost * add_qty}",
        category="inventory"
    )
    flash(f'"{item}" restocked by {add_qty} units.', 'success')
    return redirect(url_for("admin_inventory"))

# ---------------- ADD EXPENSE (manual) ----------------
@app.route("/expenses/add", methods=["POST"])
@admin_required
def add_expense():
    description = request.form["description"]
    cost        = float(request.form["cost"])
    category    = request.form["category"]
    date_str    = request.form["date"]
    date_val    = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=PH_TZ)
 
    expenses.add({
        "description": description,
        "cost": cost,
        "category": category,
        "date": date_val
    })
 
    log_admin_action(
        action="Added expense",
        target=f"{description} — ₱{cost} [{category}]",
        category="expense"
    )
    flash("Expense added!", "success")
    return redirect(url_for("admin_expenses"))
 
 
# ---------------- DELETE EXPENSE ----------------
@app.route("/expenses/delete/<id>", methods=["POST"])
@admin_required
def delete_expense(id):
    doc = expenses.document(id).get()
    desc = doc.to_dict().get("description", id) if doc.exists else id
    expenses.document(id).delete()
    log_admin_action(
        action="Deleted expense",
        target=desc,
        category="expense"
    )
    flash("Expense deleted.", "success")
    return redirect(url_for("admin_expenses"))
 
 
# ---------------- EDIT EXPENSE ----------------
@app.route("/expenses/edit/<id>", methods=["POST"])
@admin_required
def edit_expense(id):
    try:
        new_cost = float(request.form["cost"].replace(",", ""))
    except ValueError:
        flash("Invalid cost value. Please enter a number.", "danger")
        return redirect(url_for("admin_expenses"))
 
    category = request.form.get("category", "Others")
 
    expenses.document(id).update({
        "cost": new_cost,
        "category": category
    })
    log_admin_action(
        action="Edited expense",
        target=f"₱{new_cost} [{category}]",
        category="expense"
    )
    flash("Expense updated.", "success")
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
        invalidate_cache("all_cakes")
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
            return jsonify({"success": False, "message": "Cake not found!"}), 404

        existing_data = cake_doc.to_dict()
        existing_image = existing_data.get('image')
        new_image_url = existing_image  # default: keep old image

        # Only upload if a new file was actually selected
        image_file = request.files.get('image')
        if image_file and image_file.filename:
            uploaded_url = save_uploaded_image(image_file, 'cake')
            if uploaded_url:
                # Delete the old image from Cloudinary
                if existing_image:
                    delete_uploaded_image(existing_image)
                new_image_url = uploaded_url
            else:
                return jsonify({"success": False, "message": "Image upload failed. Check format (JPG/PNG) and size (max 2MB)."}), 400

        cake_ref.update({
            'name':        request.form.get('name'),
            'description': request.form.get('description'),
            'category':    request.form.get('category'),
            'price':       float(request.form.get('price')),
            'quantity':    int(request.form.get('quantity')),
            'status':      request.form.get('status') == 'on',
            'image':       new_image_url
        })
        invalidate_cache("all_cakes")
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
        invalidate_cache("all_cakes")
        log_admin_action(
            action="Deleted cake",
            target=f"{cake_name} (ID: {cake_id})",
            category="cake"
        )
        return jsonify({"success": True, "message": "Cake deleted successfully!"})
    except Exception:
        app.logger.exception(f"Error deleting cake {cake_id}")
        return jsonify({"success": False, "message": "Failed to delete cake."}), 500
    
# ---------------- EDIT CUSTOM CAKE PRICE ----------------
@app.route("/update-custom-pricing", methods=["POST"])
@admin_required
def update_custom_pricing():
    try:
        data = request.get_json()
        category = data.get("category")  # e.g. "icing", "size", "layers", "toppers", "addons"
        fields   = data.get("fields")    # e.g. {"fondant": 3500, "buttercream": 2000}

        allowed = {"icing", "size", "layers", "toppers", "addons"}
        if category not in allowed:
            return jsonify({"success": False, "message": "Invalid category"}), 400

        custom_cake_price.document(category).update(
            {k: int(v) for k, v in fields.items()}
        )
        invalidate_cache("custom_prices")

        return jsonify({"success": True, "message": f"{category.capitalize()} prices updated!"})
    except Exception as e:
        app.logger.exception("Error updating custom pricing")
        return jsonify({"success": False, "message": "Update failed"}), 500

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
@csrf.exempt
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
        
        existing = orders.where("paymongo_session_id", "==", session_id).limit(1).stream()
        if next(existing, None):
            pending_ref.delete()
            return jsonify({"status": "already processed"}), 200
        
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
        dp_type = order_data.get("downpayment_type")
        if dp_type and dp_type != "full":
            order_data["payment_status"] = "Downpayment Paid"
        else:
            order_data["payment_status"] = "Paid"
        order_data["payment_id"] = payment_id
        order_data["payment_method"] = payment_method
        order_data["paymongo_session_id"] = session_id 
        
        # Deduct stock for premade orders
        if order_data.get("order_type") == "premade":
            @firestore.transactional
            def deduct_stock(transaction, items):
                for item in items:
                    cake_id          = item.get("cake_id")
                    quantity_ordered = int(item.get("quantity", 1))
                    cake_ref         = cakes.document(cake_id)
                    cake_snap        = cake_ref.get(transaction=transaction)
                    if not cake_snap.exists:
                        raise ValueError(f"NOEXIST:{cake_id}")
                    current_qty = cake_snap.to_dict().get("quantity", 0)
                    if quantity_ordered > current_qty:
                        name = cake_snap.to_dict().get("name", "A cake")
                        raise ValueError(f"OVERSTOCK:{name}:{current_qty}")
                    new_qty = current_qty - quantity_ordered
                    transaction.update(cake_ref, {"quantity": new_qty, "status": new_qty > 0})

            try:
                transaction = db.transaction()
                deduct_stock(transaction, order_data.get("selected_items", []))
            except ValueError as e:
                parts = str(e).split(":", 2)
                app.logger.error(f"Stock deduction failed in webhook: {parts}")
                pending_ref.delete()
                return jsonify({"status": "stock error"}), 200

        # Save to orders collection
        doc_ref  = orders.add(order_data)        # was  orders.add(order_data)
        order_id = doc_ref[1].id
        users.document(order_data.get("user_id")).update({"order_count": firestore.Increment(1)})
        invalidate_cache("order_counts")
         # Confirmation email 
        user_doc = users.document(order_data.get("user_id")).get()
        fname    = user_doc.to_dict().get("fname", "Customer")
        email    = user_doc.to_dict().get("email", "")
        send_order_confirmation(
            fname=fname,
            email=email,
            order_id=order_id,
            amount=order_data.get("amount", 0),
            payment_method=payment_method,
            rush_fee=order_data.get("rush_fee", 0),
            delivery_fee=order_data.get("delivery_fee", 0),
            discount_amount=order_data.get("discount_amount", 0),
            downpayment_type=order_data.get("downpayment_type"),
            downpayment_amount=order_data.get("downpayment_amount"),
            remaining_balance=order_data.get("remaining_balance"),
        )
        # Mark voucher as used
        v_id = order_data.get('voucher_id', '')
        if v_id:
            users.document(order_data['user_id']).collection("vouchers").document(v_id).update({
                "used":    True,
                "used_at": datetime.now(PH_TZ)
            })
        # Loyalty stamp
        handle_loyalty_stamp(
            users,
            order_data.get('user_id'),
            order_data.get('order_type'),
            order_data.get('selected_items', []),
            cakes
        )
        # Delete pending order
        pending_ref.delete()
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
    # ── Set correct payment status based on downpayment type ──
    dp_type = order_data.get("downpayment_type")
    if dp_type and dp_type != "full":
        order_data["payment_status"] = "Downpayment Paid"
    else:
        order_data["payment_status"] = "Paid"
    order_data["payment_id"]     = payment_result.get("reference")
    order_data["payment_method"] = payment_result.get("payment_method", order_data["payment_method"]).upper()
    order_data["paymongo_session_id"] = session_id

    # Save to orders
    doc_ref = orders.add(order_data)
    order_id = doc_ref[1].id
    users.document(order_data.get("user_id")).update({"order_count": firestore.Increment(1)})
    invalidate_cache("order_counts")
    # send confirmation email
    user_doc = users.document(order_data.get("user_id")).get()
    fname    = user_doc.to_dict().get("fname", "Customer")
    email    = user_doc.to_dict().get("email", "")
    send_order_confirmation(
    fname=fname,
    email=email,
    order_id=order_id,
    amount=order_data.get("amount", 0),
    payment_method=order_data.get("payment_method"),
    rush_fee=order_data.get("rush_fee", 0),
    delivery_fee=order_data.get("delivery_fee", 0),
    discount_amount=order_data.get("discount_amount", 0),
    downpayment_type=order_data.get("downpayment_type"),
    downpayment_amount=order_data.get("downpayment_amount"),
    remaining_balance=order_data.get("remaining_balance"),
)
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
# ---------------- PAYMENT SUCCESS COD ----------------
@app.route("/order/success/<order_id>")
@login_required
def cod_success(order_id):
    order_doc = orders.document(order_id).get()
    if not order_doc.exists:
        return redirect(url_for("customer_dashboard"))
    order = order_doc.to_dict()
    order["id"] = order_doc.id
    order = convert_timestamps(order)
    return render_template("cod_success.html", order=order)
# ---------------- PAYMENT FAILED ----------------
@app.route("/payment/failed")
@login_required
def payment_failed():
    session_id = session.pop('paymongo_session_id', None)

    if session_id:
        pending_orders.document(session_id).delete()

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

        conv_ref = users.document(user_id).collection("conversations").document(conversation_id)
        conv_doc = conv_ref.get()

        if not conv_doc.exists:
            conv_ref.set({
                'created_at': now,
                'last_updated': now,
                'escalated': False
            })
            conv_doc = conv_ref.get()
        conv_data = conv_doc.to_dict() if conv_doc.exists else {'escalated': False}
        is_escalated = conv_data.get('escalated', False)

        if is_escalation and not is_escalated:
            conv_ref.update({
                'escalated': True,
                'escalated_at': now,
                'escalated_by': 'customer'
            })
            is_escalated = True

        # Save customer message ✅ CORRECT
        messages_ref = conv_ref.collection("messages")
        messages_ref.add({
            "text": message,
            "sender": "customer",
            "timestamp": now,
            "created_at": now
        })

        conv_ref.update({'last_updated': now})

        user_data = users.document(user_id).get().to_dict() or {}
        conversations.document(conversation_id).set({
            'user_id': user_id,
            'conversation_id': conversation_id,
            'customer_name': user_data.get('fname') or user_data.get('username') or 'Customer',
            'email': user_data.get('email') or '',
            'last_message': message[:50],
            'last_updated': now,
            'escalated': is_escalated,
            'unread': True
        }, merge=True)

        # Only send bot response if NOT escalated
        if not is_escalated:
            bot_response = (
                "✅ Thank you! The shop owner has been notified and will respond shortly. You're now chatting with the owner."
                if is_escalation
                else get_faq_response(message)
            )
            messages_ref.add({
                "text": bot_response,
                "sender": "bot",
                "timestamp": now + timedelta(seconds=1),
                "created_at": now + timedelta(seconds=1)
            })
        elif is_escalation:
            messages_ref.add({
                "text": "✅ You're now connected with the shop owner. They'll respond shortly.",
                "sender": "bot",
                "timestamp": now + timedelta(seconds=1),
                "created_at": now + timedelta(seconds=1)
            })

        return jsonify({'success': True, 'escalated': is_escalated})

    except Exception:
        app.logger.exception("Error in send_message")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500
# ---------------- RESET/START NEW CONVERSATION ----------------
@app.route('/reset-conversation', methods=['POST'])
@login_required
@limiter.limit("5 per minute")
def reset_conversation():
    try:
        data = request.get_json()
        user_id = session['user_id']
        old_conversation_id = data.get('conversation_id')

        now = datetime.now(PH_TZ)

        # Mark old conversation as closed
        if old_conversation_id:
            users.document(user_id).collection('conversations').document(old_conversation_id).update({
                'closed': True,
                'closed_at': now
            })

        # Create new conversation
        new_conv_id = f'conv_{int(now.timestamp() * 1000)}'

        users.document(user_id).collection('conversations').document(new_conv_id).set({
            'created_at': now,
            'last_updated': now,
            'escalated': False,
            'closed': False
        })

        return jsonify({'success': True, 'new_conversation_id': new_conv_id})

    except Exception:
        app.logger.exception("Error in reset_conversation")
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
            conv_doc = conv_ref.get()
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
        user_data = users.document(user_id).get().to_dict() or {}
        conversations.document(conversation_id).set({
            'user_id': user_id,
            'conversation_id': conversation_id,
            'customer_name': user_data.get('fname') or user_data.get('username') or 'Customer',
            'email': user_data.get('email') or '',
            'last_message': message[:50],
            'last_updated': now,
            'escalated': True,
            'unread': False
        }, merge=True)
        
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
# ---------------- DELETE CUSTOMER COVERSATION ----------------
@app.route('/admin/delete-conversation', methods=['POST'])
@admin_required
def delete_conversation():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        conversation_id = data.get('conversation_id')
        
        if not user_id or not conversation_id:
            return jsonify({'success': False, 'error': 'Missing data'}), 400
        
        conv_ref = users.document(user_id).collection("conversations").document(conversation_id)
        
        # Batch delete messages + top-level conversation doc (batch delete =all or nothing)
        messages = list(conv_ref.collection("messages").stream())
        batch = db.batch()
        for msg in messages:
            batch.delete(msg.reference)
        batch.delete(conv_ref)  
        batch.delete(conversations.document(conversation_id))
        batch.commit()
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
    
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        ngrok.kill()
        public_url = ngrok.connect(5000)
        print(f"\n🌐 Public URL: {public_url}\n")
    
    app.run(debug=True)