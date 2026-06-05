from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, abort
from flask_wtf.csrf import CSRFProtect, CSRFError
from flask_talisman import Talisman
from extensions import limiter, send_order_confirmation
from flask_limiter.errors import RateLimitExceeded
from datetime import datetime, timedelta, timezone
from helpers import (PH_TZ, log_admin_action, convert_timestamps, 
                     calculate_order_total, _today_range, 
                     get_faq_response, save_uploaded_image, delete_uploaded_image, 
                     handle_loyalty_stamp,safe_float, send_new_order_fcm)
from decorators import login_required, admin_required, profile_required
from utils import get_all_cakes, get_all_reviews, get_order_counts,get_custom_prices,get_loyalty_gifts,get_locked_dates_cached,get_completed_cancelled_orders,invalidate_cache,get_converted_consultations
from firebase_admin import messaging
import requests as http_requests
from webauthn import (
    generate_registration_options,
    generate_authentication_options,
    verify_registration_response,
    verify_authentication_response,
    options_to_json,
    base64url_to_bytes
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    UserVerificationRequirement,
    ResidentKeyRequirement,
    AuthenticatorAttachment,
    PublicKeyCredentialDescriptor,
    RegistrationCredential,
    AuthenticatorAttestationResponse,
    AuthenticationCredential,
    AuthenticatorAssertionResponse,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier
import base64
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
# WebAuthn config
RP_NAME   = "Mrs. Brave's Cakes"                 
RP_ID     = "acceptant-impalpable-axton.ngrok-free.dev" # change to yourdomain.com in production
RP_ORIGIN = "https://acceptant-impalpable-axton.ngrok-free.dev"       # change to https://yourdomain.com in production

app.secret_key = os.getenv('SECRET_KEY')
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024  # 2MB max file size
is_production = os.environ.get('FLASK_ENV') == 'production'
app.config['SESSION_COOKIE_SECURE'] = is_production   
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)  
PAYMONGO_WEBHOOK_SECRET = os.getenv("PAYMONGO_WEBHOOK_SECRET")

csrf = CSRFProtect(app)
@app.errorhandler(CSRFError)
def csrf_error(_):
    if 'application/json' in request.headers.get('Accept', '') or \
       'application/json' in request.headers.get('Content-Type', ''):
        return jsonify({'error': 'csrf_expired', 'message': 'Session expired.'}), 400
    return render_template('400.html'), 400
csp = {
    'default-src': ["'self'"],
    'base-uri': ["'self'"],
    'form-action': ["'self'"],
    'script-src': [
        "'self'",
        "cdn.jsdelivr.net",
        "cdnjs.cloudflare.com",
        "unpkg.com",
        "www.gstatic.com",
        "www.google.com",
        "apis.google.com",

    ],

    'style-src': [
        "'self'",
         "*.gstatic.com",
        "cdn.jsdelivr.net",
        "cdnjs.cloudflare.com",
        "unpkg.com",
        "fonts.googleapis.com",
        "'unsafe-inline'",
       
    ],

    'font-src': [
        "'self'",
        "fonts.gstatic.com",
        "cdnjs.cloudflare.com",
        "cdn.jsdelivr.net",
        "unpkg.com",
    ],

    'img-src': [
        "'self'",
        "data:",
        "*.tile.openstreetmap.org",
        "unpkg.com",
        "firebasestorage.googleapis.com",
        "lh3.googleusercontent.com",  # Google profile pictures
        "res.cloudinary.com",
    ],

    'connect-src': [
        "'self'",
        "*.gstatic.com",
        "nominatim.openstreetmap.org",
        "router.project-osrm.org",
        "firestore.googleapis.com",
        "identitytoolkit.googleapis.com",
        "securetoken.googleapis.com",
        "fcmregistrations.googleapis.com",
        "fcm.googleapis.com",
        "www.google.com",
        "firebaseinstallations.googleapis.com",
    ],

    'frame-src': [
        "'self'",
        "www.google.com",       # reCAPTCHA iframe
        "cakeshop-2faf4.firebaseapp.com",  # Firebase auth popup
    ],

    'worker-src': [
        "'self'",
        "blob:",  # service workers
    ],
}

Talisman(app,
    force_https=is_production,
    session_cookie_secure=is_production,
    session_cookie_http_only=True,
    session_cookie_samesite='Lax',
    content_security_policy=csp,
    content_security_policy_nonce_in=['script-src'],
)

limiter.init_app(app)
@app.errorhandler(RateLimitExceeded)
def handle_rate_limit(e):
    if request.is_json:  #  covers all routes
        return jsonify({"error": "Too many requests. Please slow down."}), 429
    retry_after = getattr(e, 'retry_after', 60)
    return render_template('429.html', description=e.description, retry_after=retry_after), 429

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
@app.errorhandler(400)
def bad_request(e):
    return render_template('400.html'), 400
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
@app.route('/429')
def too_many_requests():
    return render_template('429.html'), 429

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
@app.route("/terms-of-service")
def terms_of_service():
    return render_template("terms_of_service.html")
@app.route('/about')
def about():
    return render_template('about_us.html')
# ---------------- AUTHENTICATION ----------------
@app.route('/authentication')
def auth_page():
    return render_template('authentication.html',
        recaptcha_site_key=os.environ.get('RECAPTCHA_SITE_KEY')
    )
@app.route('/forgot-password')
def forgot_password_page():
    return render_template('forgot_password.html',
        recaptcha_site_key=os.environ.get('RECAPTCHA_SITE_KEY')
    )

# ---------------- LOGOUT ----------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth_page"))

# ---------------- VERIFY TOKEN ----------------
@app.route('/verify-token', methods=['POST'])
@limiter.limit("5 per minute")
def verify_token():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid request'}), 400
    id_token = data.get('idToken')
    recaptcha_token = data.get('recaptchaToken')
    # ── reCAPTCHA check ──
    if not recaptcha_token:
        return jsonify({'error': 'reCAPTCHA token missing'}), 403

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
            app.logger.info(f"reCAPTCHA: success={result.get('success')}, score={score}")
            if not result.get('success') or score < 0.5:
                app.logger.warning(f"reCAPTCHA failed: score={score}")
                return jsonify({'error': 'Suspicious activity detected.'}), 403
        except Exception as e:
            app.logger.warning(f"reCAPTCHA check failed: {e}")
            return jsonify({'error': 'reCAPTCHA verification failed'}), 403

    if not id_token:
        return jsonify({'error': 'No token provided'}), 400

    try:
        decoded_token = auth.verify_id_token(id_token,  clock_skew_seconds=10)
        uid = decoded_token['uid']
        firebase_user = auth.get_user(uid)
        if firebase_user.disabled:
            return jsonify({'error': 'Your account has been disabled. Contact support.', 'account_disabled': True}), 403

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
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid request'}), 400
        
    # ── reCAPTCHA check 
    recaptcha_token = data.get('recaptchaToken')
    if not recaptcha_token:
        return jsonify({'error': 'reCAPTCHA token missing'}), 403
    try:
        r = http_requests.post(
            'https://www.google.com/recaptcha/api/siteverify',
            data={
                'secret': os.environ.get('RECAPTCHA_SECRET_KEY'),
                'response': recaptcha_token
            },
            timeout=5
        )
        result = r.json()
        score = result.get('score', 0)
        if not result.get('success') or score < 0.5:
            app.logger.warning(f"reCAPTCHA failed on signup: score={score}")
            return jsonify({'error': 'Suspicious activity detected.'}), 403
    except Exception as e:
        app.logger.warning(f"reCAPTCHA check failed: {e}")
        return jsonify({'error': 'reCAPTCHA verification failed'}), 403
    try:
        decoded_token = auth.verify_id_token(id_token, clock_skew_seconds=10)
        token_uid = decoded_token['uid']
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
        number = number.replace(' ', '')
        if not number or not re.match(r'^(\+63|0)[0-9]{9,10}$', number):
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
            'created_at': firestore.SERVER_TIMESTAMP,
            'consent_given': True,
            'consent_date': datetime.now(PH_TZ)
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
        number = number.replace(' ', '')
        if not number or not re.match(r'^(\+63|0)[0-9]{9,10}$', number):
            flash('Invalid phone number.', 'danger')
            return redirect(url_for('complete_profile'))

        if not address or len(address) > 255:
            flash('Invalid address.', 'danger')
            return redirect(url_for('complete_profile'))
        agree = request.form.get('agreeTerms')
        if not agree:
            flash('You must agree to the Privacy Policy.', 'danger')
            return redirect(url_for('complete_profile'))
        users.document(user_id).update({
            "fname":    fname,
            "username": username,
            "number":   number,
            "address":  address,
            "consent_given": True,
            "consent_date": datetime.now(PH_TZ)
        })
        return redirect(url_for('customer_dashboard'))

    doc = users.document(user_id).get()
    customer = doc.to_dict()
    return render_template('complete_profile.html', customer=customer)
# ---------------- FINGERPRINT ROUTES ----------------
# ---------------- WEBAUTHN — REGISTER START----------------
@app.route('/webauthn/register/start', methods=['POST'])
@login_required
@limiter.limit("5 per minute")
def webauthn_register_start():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Not authenticated'}), 401

    try:
        user_doc = users.document(user_id).get()
        if not user_doc.exists:
            return jsonify({'error': 'User not found'}), 404
        user_data = user_doc.to_dict()

        options = generate_registration_options(
            rp_id=RP_ID,
            rp_name=RP_NAME,
            user_id=user_id.encode(),
            user_name=user_data.get('email', user_id),
            user_display_name=user_data.get('fname') or user_data.get('username') or 'Customer',
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            supported_pub_key_algs=[COSEAlgorithmIdentifier.ECDSA_SHA_256],
        )

        session['webauthn_reg_challenge'] = base64.b64encode(options.challenge).decode()

        return jsonify({
            "publicKey": {
                "challenge": base64.urlsafe_b64encode(options.challenge).rstrip(b'=').decode(),
                "rp": {"id": options.rp.id, "name": options.rp.name},
                "user": {
                    "id": base64.urlsafe_b64encode(options.user.id).rstrip(b'=').decode(),
                    "name": options.user.name,
                    "displayName": options.user.display_name,
                },
                "pubKeyCredParams": [{"type": p.type, "alg": p.alg} for p in options.pub_key_cred_params],
                "authenticatorSelection": {
                    "authenticatorAttachment": "platform",
                    "userVerification": "required",
                    "residentKey": "preferred",
                },
                "timeout": 60000,
                "attestation": "none",
            }
        }), 200

    except Exception:
        app.logger.exception("Error in webauthn_register_start")
        return jsonify({'error': 'Internal server error'}), 500

# ---------------- WEBAUTHN — REGISTER FINISH----------------
@app.route('/webauthn/register/finish', methods=['POST'])
@login_required
@limiter.limit("5 per minute")
def webauthn_register_finish():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Not authenticated'}), 401

    challenge_b64 = session.pop('webauthn_reg_challenge', None)
    if not challenge_b64:
        return jsonify({'error': 'No registration challenge found. Please try again.'}), 400

    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid request'}), 400

    try:
        credential = RegistrationCredential(
            id=data['id'],
            raw_id=base64url_to_bytes(data['rawId']),
            type=data.get('type', 'public-key'),
            response=AuthenticatorAttestationResponse(
                client_data_json=base64url_to_bytes(data['response']['clientDataJSON']),
                attestation_object=base64url_to_bytes(data['response']['attestationObject']),
            ),
        )

        verification = verify_registration_response(
            credential=credential,
            expected_challenge=base64.b64decode(challenge_b64),
            expected_rp_id=RP_ID,
            expected_origin=RP_ORIGIN,
            require_user_verification=True,
        )

        credential_id_b64 = base64.b64encode(verification.credential_id).decode()
        public_key_b64 = base64.b64encode(verification.credential_public_key).decode()

        db.collection('webauthn_credentials').document(user_id).set({
            'user_id': user_id,
            'credentials': firestore.ArrayUnion([{
                'credential_id': credential_id_b64,
                'public_key': public_key_b64,
                'sign_count': verification.sign_count,
                'created_at': datetime.now(PH_TZ).isoformat(),
            }])
        }, merge=True)

        app.logger.info(f"WebAuthn credential registered for user {user_id}")
        return jsonify({'success': True}), 200

    except Exception:
        app.logger.exception("Error in webauthn_register_finish")
        return jsonify({'error': 'Registration verification failed'}), 400

# ---------------- WEBAUTHN — LOGIN START----------------
@app.route('/webauthn/login/start', methods=['POST'])
@limiter.limit("10 per minute")
def webauthn_login_start():
    try:
        creds_docs = db.collection('webauthn_credentials').stream()
        allow_credentials = []
        for doc in creds_docs:
            d = doc.to_dict()
            for c in d.get('credentials', []):
                allow_credentials.append(
                    PublicKeyCredentialDescriptor(id=base64.b64decode(c['credential_id']))
                )

        options = generate_authentication_options(
            rp_id=RP_ID,
            allow_credentials=allow_credentials,
            user_verification=UserVerificationRequirement.REQUIRED,
        )

        session['webauthn_auth_challenge'] = base64.b64encode(options.challenge).decode()

        return jsonify({
            "publicKey": {
                "challenge": base64.urlsafe_b64encode(options.challenge).rstrip(b'=').decode(),
                "timeout": 60000,
                "rpId": RP_ID,
                "allowCredentials": [
                    {
                        "type": "public-key",
                        "id": base64.urlsafe_b64encode(c.id).rstrip(b'=').decode()
                    }
                    for c in options.allow_credentials
                ],
                "userVerification": "required"
            }
        }), 200

    except Exception:
        app.logger.exception("Error in webauthn_login_start")
        return jsonify({'error': 'Internal server error'}), 500

# ---------------- WEBAUTHN — LOGIN FINISH----------------
@app.route('/webauthn/login/finish', methods=['POST'])
@limiter.limit("10 per minute")
def webauthn_login_finish():
    challenge_b64 = session.pop('webauthn_auth_challenge', None)
    if not challenge_b64:
        return jsonify({'error': 'No authentication challenge found. Please try again.'}), 400

    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid request'}), 400

    try:
        cred_id_b64 = base64.b64encode(base64url_to_bytes(data.get('id'))).decode()

        # find user doc containing this credential
        cred_doc = None
        matched_cred = None
        for doc in db.collection('webauthn_credentials').stream():
            d = doc.to_dict()
            for c in d.get('credentials', []):
                if c['credential_id'] == cred_id_b64:
                    cred_doc = doc
                    matched_cred = c
                    break
            if cred_doc:
                break

        if not cred_doc or not matched_cred:
            return jsonify({'error': 'Credential not found. Please register your device first.'}), 404

        cred_data = cred_doc.to_dict()
        user_id = cred_data['user_id']
        stored_public_key = base64.b64decode(matched_cred['public_key'])
        stored_sign_count = matched_cred.get('sign_count', 0)

        credential = AuthenticationCredential(
            id=data['id'],
            raw_id=base64url_to_bytes(data['rawId']),
            type=data.get('type', 'public-key'),
            response=AuthenticatorAssertionResponse(
                client_data_json=base64url_to_bytes(data['response']['clientDataJSON']),
                authenticator_data=base64url_to_bytes(data['response']['authenticatorData']),
                signature=base64url_to_bytes(data['response']['signature']),
                user_handle=base64url_to_bytes(data['response']['userHandle']) if data['response'].get('userHandle') else None,
            ),
        )

        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=base64.b64decode(challenge_b64),
            expected_rp_id=RP_ID,
            expected_origin=RP_ORIGIN,
            credential_public_key=stored_public_key,
            credential_current_sign_count=stored_sign_count,
            require_user_verification=True,
        )

        # update sign count for matched credential only
        updated_creds = [
            {**c, 'sign_count': verification.new_sign_count} if c['credential_id'] == cred_id_b64 else c
            for c in cred_data.get('credentials', [])
        ]
        cred_doc.reference.update({'credentials': updated_creds})

        user_doc = users.document(user_id).get()
        if not user_doc.exists:
            return jsonify({'error': 'User account not found'}), 404

        user_data = user_doc.to_dict()
        firebase_user = auth.get_user(user_id)
        if firebase_user.disabled:
            return jsonify({'error': 'Your account has been disabled. Contact support.'}), 403

        fname = user_data.get('fname', '')
        email = user_data.get('email', '')
        is_admin = user_data.get('role') == 'admin'

        session['user'] = {'uid': user_id, 'email': email, 'name': fname or email, 'admin': is_admin}
        session['user_id'] = user_id
        session['username'] = email

        app.logger.info(f"WebAuthn login successful for user {user_id}")
        return jsonify({'success': True, 'redirect': '/admin/dashboard' if is_admin else '/customer_dashboard'}), 200

    except Exception:
        app.logger.exception("Error in webauthn_login_finish")
        return jsonify({'error': 'Authentication verification failed'}), 400



# ---------------- WEBAUTHN — CHECK-----------------
@app.route('/webauthn/check', methods=['GET'])
@limiter.limit("20 per minute")
def webauthn_check():
    user_id = session.get('user_id')
    try:
        if user_id:
            doc = db.collection('webauthn_credentials').document(user_id).get()
            if not doc.exists:
                return jsonify({'registered': False}), 200
            creds = doc.to_dict().get('credentials', [])
            return jsonify({'registered': len(creds) > 0}), 200
        else:
            creds = db.collection('webauthn_credentials').limit(1).stream()
            has_any = any(True for _ in creds)
            return jsonify({'registered': has_any}), 200
    except Exception:
        return jsonify({'registered': False}), 200


# ---------------- WEBAUTHN — DELETE----------------
@app.route('/webauthn/delete', methods=['POST'])
@login_required
@limiter.limit("5 per minute")
def webauthn_delete():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Not authenticated'}), 401
    try:
        db.collection('webauthn_credentials').document(user_id).delete()
        return jsonify({'success': True}), 200
    except Exception:
        app.logger.exception("Error deleting WebAuthn credential")
        return jsonify({'error': 'Internal server error'}), 500
# ---------------- FORGOT PASS RECAPTCHA----------------
@app.route('/verify-recaptcha', methods=['POST'])
@limiter.limit("5 per minute")
def verify_recaptcha():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid request'}), 400

    recaptcha_token = data.get('recaptchaToken')
    if not recaptcha_token:
        return jsonify({'error': 'reCAPTCHA token missing'}), 403

    try:
        r = http_requests.post(
            'https://www.google.com/recaptcha/api/siteverify',
            data={
                'secret': os.environ.get('RECAPTCHA_SECRET_KEY'),
                'response': recaptcha_token
            },
            timeout=5
        )
        result = r.json()
        score = result.get('score', 0)
        app.logger.info(f"reCAPTCHA forgot_password: success={result.get('success')}, score={score}")
        if not result.get('success') or score < 0.5:
            app.logger.warning(f"reCAPTCHA failed: score={score}")
            return jsonify({'error': 'Suspicious activity detected.'}), 403
    except Exception as e:
        app.logger.warning(f"reCAPTCHA check failed: {e}")
        return jsonify({'error': 'reCAPTCHA verification failed'}), 403

    return jsonify({'success': True}), 200

@app.route('/check-email-exists', methods=['POST'])
@limiter.limit("5 per minute")  
def check_email_exists():
    data = request.get_json()
    email = data.get('email', '').strip().lower()
    
    if not email:
        return jsonify({'error': 'Email required'}), 400

    try:
        auth.get_user_by_email(email)
        return jsonify({'exists': True}), 200
    except auth.UserNotFoundError:
        return jsonify({'exists': False}), 200
    except Exception:
        return jsonify({'error': 'Server error'}), 500
# ---------------- DELETE ACCOUNT ----------------
@app.route('/delete-account', methods=['POST'])
@login_required
@limiter.limit("3 per hour")
def delete_account():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json()
    id_token = data.get('idToken')
    if not id_token:
        return jsonify({'error': 'Missing token'}), 400

    try:
        # Verify token
        decoded = auth.verify_id_token(id_token, clock_skew_seconds=10)
        if decoded['uid'] != user_id:
            return jsonify({'error': 'UID mismatch'}), 403

        # Check active orders
        active_statuses = ['New', 'Accepted', 'Pending', 'Ready', 'Out for Delivery']
        active_orders = orders.where('user_id', '==', user_id)\
                               .where('status', 'in', active_statuses)\
                               .get()

        if len(active_orders) > 0:
            return jsonify({
                'error': 'You have active orders. Please wait until all orders are completed or cancelled before deleting your account.'
            }), 400

        # Anonymize order history
        all_orders = orders.where('user_id', '==', user_id).get()
        for order_doc in all_orders:
            order_doc.reference.update({
                'user_id': 'deleted',
                'customer': {
                    'name': 'Deleted User',
                    'contact': '',
                    'address': '',
                    'age': '',
                    'celebrant': '',
                    'occasion': '',
                    'lat': None,
                    'lng': None
                }
            })

        # Delete Firestore user document
        users.document(user_id).delete()

        # Delete Firebase Auth account
        auth.delete_user(user_id)

        # Clear session
        session.clear()

        return jsonify({'success': True}), 200

    except auth.InvalidIdTokenError:
        return jsonify({'error': 'Invalid token'}), 401
    except auth.ExpiredIdTokenError:
        return jsonify({'error': 'Token expired'}), 401
    except Exception:
        app.logger.exception("Unexpected error in delete_account")
        return jsonify({'error': 'Internal server error'}), 500
# ================================================================
# CUSTOMER ROUTES
# ================================================================

# ---------------- CUSTOMER DASHBOARD ----------------
@app.route("/customer_dashboard")
@profile_required
@login_required
def customer_dashboard():
    user_id = session.get("user_id")
    gifts = get_loyalty_gifts()
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
    loyalty_unclaimed_tier = customer.get('loyalty_unclaimed_tier', None)
    loyalty_stamps_for_tier = loyalty_stamps

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
        loyalty_unclaimed_tier  = customer.get('loyalty_unclaimed_tier', None),
        loyalty_gifts_small = gifts["small"],
        loyalty_gifts_big   = gifts["big"]
    )
# ---------------- CUSTOMER TRACKING ----------------
@app.route("/track/<order_id>")
@login_required
def track_order(order_id):
    order_doc = orders.document(order_id).get()
    if not order_doc.exists:
        abort(404)
    order = order_doc.to_dict()
    order['id'] = order_doc.id
    if order.get('status') not in ('Out for Delivery', 'Delivered'):
        abort(404)
    # Make sure it belongs to logged-in customer
    if order.get('user_id') != session.get('user_id'):
        abort(403)
    return render_template('customer_track.html', order=order)
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
    
    number = number.replace(' ', '')
    if not number or not re.match(r'^(\+63|0)[0-9]{9,10}$', number):
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

# ── Hardcoded gift lists (change to dynamic later) ──
LOYALTY_GIFTS_SMALL = ["Cupcake", "Coffee", "Pastry"]          # 5 stamps
LOYALTY_GIFTS_BIG   = ["Bento Cake", "Slice Cake", "Drinks Bundle"]  # 10 stamps

# ---------------- LOYALTY CLAIM GIFT ----------------
@app.route("/loyalty/claim", methods=["POST"])
@profile_required
@login_required
@limiter.limit("3 per minute")
def loyalty_claim():
    user_id   = session.get("user_id")
    now       = datetime.now(PH_TZ)
    gift_name = request.form.get("gift_name", "").strip()
    tier_raw  = request.form.get("tier", "0")
    tier      = int(tier_raw) if tier_raw and tier_raw.isdigit() else 0

    if tier not in (5, 10):
        flash("Invalid reward tier.", "warning")
        return redirect(url_for("customer_dashboard") + "#loyalty")

    gifts      = get_loyalty_gifts()
    valid_gifts = gifts["small"] if tier == 5 else gifts["big"]

    if gift_name not in valid_gifts:
        flash("Invalid gift selection.", "warning")
        return redirect(url_for("customer_dashboard") + "#loyalty")

    try:
        user_ref  = users.document(user_id)
        user_data = user_ref.get().to_dict() or {}
        unclaimed      = user_data.get("loyalty_unclaimed", False)
        unclaimed_tier = user_data.get("loyalty_unclaimed_tier")

        if not unclaimed or unclaimed_tier != tier:
            flash("You haven't reached this reward milestone yet.", "warning")
            return redirect(url_for("customer_dashboard") + "#loyalty")
        stamps    = int(user_data.get("loyalty_stamps", 0))

        if stamps < tier:
            flash("Not enough stamps to claim this reward.", "warning")
            return redirect(url_for("customer_dashboard") + "#loyalty")
        expires_at = now + timedelta(days=180)

        user_ref.collection("vouchers").add({
            "gift_name":  gift_name,
            "tier":       tier,
            "type":       "gift",
            "claimed_at": now,
            "expires_at": expires_at,
            "used":       False,
            "used_at":    None,
        })
        update_data = {
            "loyalty_unclaimed": None,
            "loyalty_unclaimed_tier": None,
            "loyalty_stamps": max(0, stamps - tier)
        }
        user_ref.update(update_data)
        flash(f"Your free {gift_name} has been claimed! Show this at the shop or use it during your next order!.", "success")

    except Exception:
        app.logger.exception("[LOYALTY CLAIM GIFT] Failed")
        flash("Something went wrong. Please try again.", "danger")

    return redirect(url_for("customer_dashboard") + "#loyalty")
@app.route("/admin/update-loyalty-gifts", methods=["POST"])
@admin_required
def update_loyalty_gifts():
    try:
        data = request.get_json()
        tier = data.get("tier")
        gifts_raw = data.get("gifts")

        if tier not in ("small", "big"):
            return jsonify({"success": False, "message": "Invalid tier."})

        # Accept either list or comma string
        if isinstance(gifts_raw, list):
            gift_list = [str(g).strip() for g in gifts_raw if str(g).strip()]
        else:
            gift_list = [g.strip() for g in gifts_raw.split(",") if g.strip()]

        if not gift_list or len(gift_list) > 10:
            return jsonify({"success": False, "message": "Please enter 1–10 gifts."})

        from db import loyalty_gifts
        docs = list(loyalty_gifts.limit(1).stream())
        if not docs:
            # Create first document with defaults
            default_data = {"small": ["Cupcake", "Coffee", "Pastry"], "big": ["Bento Cake", "Slice Cake", "Drinks Bundle"]}
            default_data[tier] = gift_list
            loyalty_gifts.add(default_data)
        else:
            doc_ref = docs[0].reference
            doc_ref.update({tier: gift_list})

        invalidate_cache("loyalty_gifts")
        return jsonify({"success": True, "message": "Gift list updated!"})
    except Exception:
        app.logger.exception("[LOYALTY GIFTS] Update failed")
        return jsonify({"success": False, "message": "Something went wrong."})

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
    # ── Consult token + image handling ──
    consult_token = request.form.get('consult_token', '').strip()
    if consult_token:
        token_ref = db.collection('pending_consultations').document(consult_token)
        token_doc = token_ref.get()
        if token_doc.exists and not token_doc.to_dict().get('used'):
            token_ref.update({'used': True, 'used_at': datetime.now(PH_TZ)})
        # ✅ carry image from consultation, no re-upload needed
        inspo_image = token_doc.to_dict().get('consultation_data', {}).get('inspo_image') if token_doc.exists else None
    else:
        # ✅ normal flow
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
    selected_json = request.form.get('selected_items', '[]')
    try:
        selected_items = json.loads(selected_json)
        if not isinstance(selected_items, list):
            raise ValueError("must be a list")
        for item in selected_items:
            if not isinstance(item, dict) or 'cake_id' not in item:
                raise ValueError("invalid item")
            qty = int(item.get('quantity', 1))
            if qty < 1 or qty > 999:
                raise ValueError("invalid quantity")
    except (json.JSONDecodeError, ValueError):
        flash("Invalid order data.", "danger")
        return redirect(url_for("customer_dashboard"))

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
        selected_items = json.loads(selected_json)
        if not isinstance(selected_items, list):
            raise ValueError("must be a list")
        for item in selected_items:
            if not isinstance(item, dict) or 'cake_id' not in item:
                raise ValueError("invalid item")
            qty = int(item.get('quantity', 1))
            if qty < 1 or qty > 999:
                raise ValueError("invalid quantity")
    except (json.JSONDecodeError, ValueError):
        flash("Invalid order data.", "danger")
        return redirect(url_for("customer_dashboard"))

    inspo_image_raw = request.form.get("inspo_image", "").strip()
    if inspo_image_raw and not inspo_image_raw.startswith("https://res.cloudinary.com/"):
        flash("Invalid image reference.", "danger")
        return redirect(url_for("customer_dashboard"))

    if delivery_type == "Delivery" and not address:
        flash("Please provide a delivery address.", "warning")
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
    if not idempotency_key or len(idempotency_key) < 16:
        flash("Invalid request.", "danger")
        return redirect(url_for("customer_dashboard"))

    existing = orders.where("idempotency_key", "==", idempotency_key)\
                    .where("user_id", "==", user_id)\
                    .limit(1).stream()
    if next(existing, None):
        flash("Order already placed.", "warning")
        return redirect(url_for("customer_dashboard"))
    claim_voucher_ids = request.form.getlist("claim_voucher_ids")
    claimed_vouchers = []
    if claim_voucher_ids:
        now_dt = datetime.now(PH_TZ)
        for v_id in claim_voucher_ids:
            v_doc = users.document(user_id).collection("vouchers").document(v_id).get()
            if not v_doc.exists:
                continue
            v = v_doc.to_dict()
            expires = v.get('expires_at')
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=PH_TZ)
            if not v.get('used', False) and expires > now_dt and v.get('type') == 'gift':
                claimed_vouchers.append({
                    "voucher_id": v_id,
                    "gift_name":  v.get("gift_name", "")
                })
    downpayment_type   = None   
    downpayment_amount = None
    remaining_balance  = None
    if order_type == "custom":
        raw_dp_type = request.form.get("downpayment_type", "full").strip()
        if raw_dp_type not in ("50", "75", "full"):
            raw_dp_type = "full"
        downpayment_type = raw_dp_type
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

    contact = contact.replace(' ', '')
    if not contact or not re.match(r'^(\+63|0)[0-9]{9,10}$', contact):
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
            amount += float(icing_prices.get(icing_key, 0))
            amount += float(size_prices.get(size_key, 0))
            amount += float(layers_prices.get(layers_key, 0))
            amount += float(toppers_prices.get(toppers_key, 0))

            for addon_key in ["filling", "cupcake", "ediblepaper", "fondanttoppers", "sprinkles", "drip", "flowers"]:
                if request.form.get(addon_key):
                    amount += float(addon_prices.get(addon_key, 0))

        except Exception as e:
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
    #  Rush fee (custom only) 
    RUSH_FEE = 300.0
    if rush:
        amount = round(amount + RUSH_FEE, 2)
    #  Add delivery fee 
    def get_delivery_fee(lat, lng):
        if lat is None or lng is None:
            return 50.0
        import math
        SHOP_LAT, SHOP_LNG = 10.711925117255893, 122.53996450415457
        # Haversine straight-line (server has no OSRM access easily)
        R = 6371
        d_lat = math.radians(lat - SHOP_LAT)
        d_lng = math.radians(lng - SHOP_LNG)
        a = math.sin(d_lat/2)**2 + math.cos(math.radians(SHOP_LAT)) * math.cos(math.radians(lat)) * math.sin(d_lng/2)**2
        km = R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

        if km <= 3:   return 50.0
        elif km <= 7: return round(50 + (km - 3) * 12, 2)
        else:         return round(50 + (4 * 12) + (km - 7) * 15, 2)

    cust_lat = safe_float(request.form.get("lat"), -90, 90)
    cust_lng = safe_float(request.form.get("lng"), -180, 180)
    DELIVERY_FEE = get_delivery_fee(cust_lat, cust_lng) if delivery_type == "Delivery" else 0.0
    if delivery_type == "Delivery":
        amount = round(amount + DELIVERY_FEE, 2)

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
        "delivery_token": secrets.token_urlsafe(32),
        "idempotency_key": idempotency_key,
        "downpayment_type":   downpayment_type,
        "claimed_vouchers": claimed_vouchers,
        "downpayment_amount": None,  # filled below after amount is final
        "remaining_balance":  None,
        "customer": {
            "name":      customer_name,
            "contact":   contact,
            "address":   address,
            "occasion":  occasion,
            "celebrant": celebrant,
            "age":       age,
            "lat": safe_float(request.form.get("lat"), -90, 90),
            "lng": safe_float(request.form.get("lng"), -180, 180),
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

    # COD  save immediately
    if payment_method == "Cash on Delivery":
        doc_ref  = orders.add(order_data)
        order_id = doc_ref[1].id
        users.document(user_id).update({"order_count": firestore.Increment(1)})
        invalidate_cache("order_counts", "all_cakes")  # cache reset
        try:
            send_new_order_fcm(
                db_ref=db,
                order_id=order_id,
                customer_name=customer_name,
                order_type=order_type,
                rush=order_data.get('rush', False)
            )
        except Exception:
            app.logger.warning('[FCM] New order notify failed (COD), non-critical')
        
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
            discount_amount=0,
            downpayment_type=order_data.get("downpayment_type"),
            downpayment_amount=order_data.get("downpayment_amount"),
            remaining_balance=order_data.get("remaining_balance"),
        )
        for cv in claimed_vouchers:
            users.document(user_id).collection("vouchers").document(cv["voucher_id"]).update({
                "used": True,
                "used_at": now
            })
        handle_loyalty_stamp(users, user_id, order_type, selected_items, cakes, order_id=order_id)
        return redirect(url_for("cod_success", order_id=order_id))

    # ── Online Payment → PayMongo ──
    line_items = build_line_items(
        order_type         = order_type,
        selected_items     = selected_items,
        amount             = amount,           # full discounted amount (for premade line items)
        downpayment_type   = downpayment_type,
        downpayment_amount = order_data.get("downpayment_amount"),
        remaining_balance  = order_data.get("remaining_balance"),
        discount_amount    = 0,
        delivery_fee       = order_data.get("delivery_fee", 0),
        rush_fee           = order_data.get("rush_fee", 0),
    )

    base_url    = request.host_url.rstrip('/')
    success_url = f"{base_url}/payment/success"
    cancel_url  = f"{base_url}/payment/failed"
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

    order_ref = orders.document(order_id)
    order_doc = order_ref.get()

    if not order_doc.exists:
        flash("Order not found.", "danger")
        return redirect(url_for("customer_dashboard"))

    order = order_doc.to_dict()

    if order.get("user_id") != user_id:
        flash("Unauthorized access.", "danger")
        return redirect(url_for("customer_dashboard"))

    if order.get("status") != "New":
        flash("Order cannot be cancelled anymore.", "warning")
        return redirect(url_for("customer_dashboard"))

    # Restore stock if premade
    if order.get("order_type") == "premade":
        for item in order.get("selected_items", []):
            cake_ref = cakes.document(item["cake_id"])
            cake_doc = cake_ref.get()
            if cake_doc.exists:
                current_qty = cake_doc.to_dict().get("quantity", 0)
                restore_qty = int(item.get("quantity", 1))
                cake_ref.update({"quantity": current_qty + restore_qty, "status": True})

    cancel_reason = request.form.get("cancel_reason", "").strip()
    cancel_reason_other = request.form.get("cancel_reason_other", "").strip()

    order_ref.update({
        "status": "Cancelled",
        "cancel_reason": cancel_reason,
        "cancel_reason_other": cancel_reason_other if cancel_reason == "Other" else "",
        "cancelled_by": "customer",
        "cancelled_at": datetime.now(PH_TZ),
    })

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

@app.route("//order-map/<order_id>")
@admin_required
def order_map(order_id):
    order_doc = orders.document(order_id).get()
    if not order_doc.exists:
        return "Order not found", 404
    order = order_doc.to_dict()
    order = convert_timestamps(order)
    return render_template("admin_orders_map.html", order=order)
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
    from collections import defaultdict

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
    alltime_data = {}

    # ── Summary cards ──
    total_revenue   = 0
    total_expenses  = 0
    total_orders    = 0
    total_completed = 0
    payment_counts  = {}
    premade_sales   = {}

    # ── KPI accumulators ──
    user_order_counts = defaultdict(int)
    user_order_spend  = defaultdict(float)
    user_order_names  = {}
    peak_days         = defaultdict(int)
    custom_revenue    = 0
    premade_revenue   = 0
    rush_count        = 0
    cancel_reasons    = defaultdict(int)
    cancelled_by_data = {"customer": 0, "admin": 0}
    total_cancelled   = 0
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
            if date_val >= week_ago:
                weekly_expenses[date_val.strftime("%a")] += cost
            if date_val.year == now.year:
                monthly_expenses[date_val.strftime("%b")] += cost
            key = date_val.strftime("%b %Y")
            if key not in alltime_data:
                alltime_data[key] = {"sales": 0, "expenses": 0, "profit": 0, "sort": date_val}
            alltime_data[key]["expenses"] += cost

    # ── Fetch orders (single loop) ──
    for order_doc in orders.stream():
        order      = order_doc.to_dict()
        status     = order.get("status", "")
        amount     = float(order.get("amount", 0))
        order_type = order.get("order_type", "")
        uid        = order.get("user_id")

        total_orders += 1
        if status == "Completed":
            total_completed += 1

        # Payment methods
        payment = order.get("payment_method", "Unknown")
        payment_counts[payment] = payment_counts.get(payment, 0) + 1

        # Premade cake popularity
        if order_type == "premade":
            for item in order.get("selected_items", []):
                name = item.get("cake_name", "Unknown")
                premade_sales[name] = premade_sales.get(name, 0) + 1

        # Rush count
        if order.get("rush"):
            rush_count += 1

        if status == "Cancelled":
            total_cancelled += 1
            reason = order.get("cancel_reason") or "No reason"
            cancel_reasons[reason] += 1
            cancelled_by = order.get("cancelled_by", "unknown")
            if cancelled_by in ("customer", "admin"):
                cancelled_by_data[cancelled_by] += 1
        # created_at — parse once, reuse below
        created_at = order.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
            else:
                created_at = created_at.astimezone(PH_TZ)
            peak_days[created_at.strftime("%A")] += 1

        # Revenue + charts (Completed or Pickup)
        if status in ["Completed", "Pickup"]:
            total_revenue += amount

            # KPI: revenue split
            if order_type == "custom":
                custom_revenue += amount
            elif order_type == "premade":
                premade_revenue += amount

            # KPI: top customers
            if uid:
                user_order_counts[uid] += 1
                user_order_spend[uid]  += amount
                if uid not in user_order_names:
                    user_order_names[uid] = order.get("customer", {}).get("name", "Unknown")

            if isinstance(created_at, datetime):
                if created_at >= week_ago:
                    weekly_sales[created_at.strftime("%a")] += amount
                if created_at.year == now.year:
                    monthly_sales[created_at.strftime("%b")] += amount
                key = created_at.strftime("%b %Y")
                if key not in alltime_data:
                    alltime_data[key] = {"sales": 0, "expenses": 0, "profit": 0, "sort": created_at}
                alltime_data[key]["sales"] += amount

    # ── Profits ──
    weekly_profit = {day: max(0, weekly_sales[day] - weekly_expenses[day]) for day in days_order}
    monthly_profit = {m: max(0, monthly_sales[m] - monthly_expenses[m]) for m in months}
    for key in alltime_data:
        alltime_data[key]["profit"] = max(0, alltime_data[key]["sales"] - alltime_data[key]["expenses"])

    alltime_sorted       = sorted(alltime_data.items(), key=lambda x: x[1]["sort"])
    alltime_labels       = [k for k, v in alltime_sorted]
    alltime_sales_vals   = [v["sales"]    for k, v in alltime_sorted]
    alltime_expense_vals = [v["expenses"] for k, v in alltime_sorted]
    alltime_profit_vals  = [v["profit"]   for k, v in alltime_sorted]

    # Top 3 premade cakes
    top_premade        = sorted(premade_sales.items(), key=lambda x: x[1], reverse=True)[:3]
    top_premade_names  = [c[0] for c in top_premade]
    top_premade_counts = [c[1] for c in top_premade]

    # Net profit / stats
    net_profit      = total_revenue - total_expenses
    completion_rate = round((total_completed / total_orders * 100), 1) if total_orders > 0 else 0
    avg_order_value = round(total_revenue / total_completed, 2)        if total_completed > 0 else 0

    # ── KPI calculations ──
    walkin_count   = sum(1 for _ in walkin_orders.stream())
    total_online_customers = len(user_order_counts)
    repeat_customers       = sum(1 for c in user_order_counts.values() if c >= 2)
    repeat_rate            = round(repeat_customers / total_online_customers * 100, 1) if total_online_customers > 0 else 0
    rush_pct               = round(rush_count / total_orders * 100, 1) if total_orders > 0 else 0
    peak_day               = max(peak_days, key=peak_days.get) if peak_days else "N/A"
    total_rev_split        = custom_revenue + premade_revenue
    custom_pct             = round(custom_revenue  / total_rev_split * 100, 1) if total_rev_split > 0 else 0
    premade_pct            = round(premade_revenue / total_rev_split * 100, 1) if total_rev_split > 0 else 0
    top_customers          = sorted(
        [{"name": user_order_names.get(uid, "Unknown"), "spend": spend, "orders": user_order_counts[uid]}
         for uid, spend in user_order_spend.items()],
        key=lambda x: x["spend"], reverse=True
    )[:5]

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
        # KPIs
        repeat_rate            = repeat_rate,
        repeat_customers       = repeat_customers,
        total_online_customers = total_online_customers,
        rush_count             = rush_count,
        rush_pct               = rush_pct,
        peak_day               = peak_day,
        top_customers          = top_customers,
        custom_revenue         = custom_revenue,
        premade_revenue        = premade_revenue,
        custom_pct             = custom_pct,
        premade_pct            = premade_pct,
        walkin_count           = walkin_count,
        online_count           = total_orders,
        cancel_reasons    = dict(cancel_reasons),
        cancelled_by_data = cancelled_by_data,
        total_cancelled   = total_cancelled,
    )
# ---------------- ADMIN CAKES ----------------
@app.route("/admin/cakes")
@admin_required
def admin_cakes():
    cakes_list    = get_all_cakes()
    custom_prices = get_custom_prices()
    loyalty_gifts = get_loyalty_gifts()
    return render_template("admin_cakes.html",
        cakes=cakes_list,
        custom_prices=custom_prices,
        loyalty_gifts=loyalty_gifts
    )

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


@app.route('/admin/consultations')
@admin_required
def admin_consultations():
    # Pending — always fresh
    pending_docs = conversations.where('is_consultation', '==', True).order_by('last_updated', direction=firestore.Query.DESCENDING).stream()
    
    consultations = []
    for doc in pending_docs:
        d = doc.to_dict()
        if d.get('status') == 'converted':
            continue  # skip converted here, handled by cache
        d['conversation_id'] = doc.id
        consultations.append(d)

    # Converted — from cache
    consultations += get_converted_consultations()

    # Sort combined list
    consultations.sort(key=lambda x: x.get('last_updated') or 0, reverse=True)

    return render_template('admin_consultations.html', consultations=consultations)
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
            if new_status == "Cancelled":
                update_data["cancel_reason"]       = request.form.get("cancel_reason", "").strip()
                update_data["cancel_reason_other"] = request.form.get("cancel_reason_other", "").strip() if request.form.get("cancel_reason") == "Other" else ""
                update_data["cancelled_by"]        = "admin"
                update_data["cancelled_at"]        = datetime.now(PH_TZ)
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

@app.route("/admin/orders/history")
@admin_required
def admin_orders_history():
    data = get_completed_cancelled_orders()
    return jsonify({"orders": data})

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
    auth.revoke_refresh_tokens(uid) 
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
        try:
            send_new_order_fcm(
                db_ref=db,
                order_id=order_id,
                customer_name=order_data.get('customer', {}).get('name', 'Customer'),
                order_type=order_data.get('order_type', 'custom'),
                rush=order_data.get('rush', False)
            )
        except Exception:
            app.logger.warning('[FCM] New order notify failed (webhook), non-critical')
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
            discount_amount=0,
            downpayment_type=order_data.get("downpayment_type"),
            downpayment_amount=order_data.get("downpayment_amount"),
            remaining_balance=order_data.get("remaining_balance"),
        )
        # Mark voucher as used
        for cv in order_data.get("claimed_vouchers", []):
            users.document(order_data["user_id"]).collection("vouchers").document(cv["voucher_id"]).update({
                "used":    True,
                "used_at": datetime.now(PH_TZ)
            })
        # Loyalty stamp
        handle_loyalty_stamp(
            users,
            order_data.get('user_id'),
            order_data.get('order_type'),
            order_data.get('selected_items', []),
            cakes,
            order_id=order_id
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
    try:
        send_new_order_fcm(
            db_ref=db,
            order_id=order_id,
            customer_name=order_data.get('customer', {}).get('name', 'Customer'),
            order_type=order_data.get('order_type', 'custom'),
            rush=order_data.get('rush', False)
        )
    except Exception:
        app.logger.warning('[FCM] New order notify failed (payment_success), non-critical')
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
    discount_amount=0,
    downpayment_type=order_data.get("downpayment_type"),
    downpayment_amount=order_data.get("downpayment_amount"),
    remaining_balance=order_data.get("remaining_balance"),
)
    handle_loyalty_stamp(
        users,
        order_data.get('user_id'),
        order_data.get('order_type'),
        order_data.get('selected_items', []),
        cakes,
        order_id=order_id
    )
        # ── Mark voucher as used ──
    for cv in order_data.get("claimed_vouchers", []):
        users.document(order_data["user_id"]).collection("vouchers").document(cv["voucher_id"]).update({
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
# ---------------- CUSTOMER SEND MESSAGE ----------------
@app.route('/send-message', methods=['POST'])
@limiter.limit("20 per minute")
def send_message():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        message = data.get('message', '').strip()
        conversation_id = data.get('conversation_id')
        is_escalation = data.get('is_escalation', False)
        order_context = data.get('order_context')  # ← NEW

        if not message:
            return jsonify({'success': False, 'error': 'Missing data'}), 400

        # Guest user — just return bot reply, skip Firestore
        if not user_id or user_id == 'guest':
            bot_response = get_faq_response(message)
            return jsonify({'success': True, 'reply': bot_response})

        if not conversation_id:
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

        # ← NEW: auto-escalate if customer sends with order_context
        if order_context and not is_escalated:
            conv_ref.update({
                'escalated': True,
                'escalated_at': now,
                'escalated_by': 'customer'
            })
            is_escalated = True
            is_escalation = True

        messages_ref = conv_ref.collection("messages")

        # Only save to Firestore if escalated
        if is_escalated:
            messages_ref.add({
                "text": message,
                "sender": "customer",
                "timestamp": now,
                "created_at": now,
                "order_context": order_context  # ← NEW
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

            if is_escalation:
                messages_ref.add({
                    "text": "✅ You're now connected with the shop owner. They'll respond shortly.",
                    "sender": "bot",
                    "timestamp": now + timedelta(seconds=1),
                    "created_at": now + timedelta(seconds=1)
                })

            return jsonify({'success': True, 'escalated': is_escalated})

        # Not escalated — just return bot reply directly, nothing saved
        if is_escalation:
            conv_ref.update({
                'escalated': True,
                'escalated_at': now,
                'escalated_by': 'customer'
            })
            messages_ref.add({
                "text": message,
                "sender": "customer",
                "timestamp": now,
                "created_at": now,
                "order_context": order_context  # ← NEW
            })
            messages_ref.add({
                "text": "✅ You're now connected with the shop owner. They'll respond shortly.",
                "sender": "bot",
                "timestamp": now + timedelta(seconds=1),
                "created_at": now + timedelta(seconds=1)
            })
            conv_ref.update({'last_updated': now})
            return jsonify({'success': True, 'escalated': True})

        bot_response = get_faq_response(message)
        return jsonify({'success': True, 'escalated': False, 'reply': bot_response})
    except Exception:
        app.logger.exception("Error in send_message")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500
    
# ---------------- ADMIN START CONVERSATION VIA ORDER ----------------
@app.route('/admin/initiate-conversation', methods=['POST'])
@admin_required
def admin_initiate_conversation():
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        message = data.get('message', '').strip()

        if not user_id or not message:
            return jsonify({'success': False, 'error': 'Missing data'}), 400

        now = datetime.now(PH_TZ)

        # Fetch customer data from users collection
        user_data = users.document(user_id).get().to_dict() or {}
        customer_name = user_data.get('fname') or user_data.get('username') or 'Customer'
        customer_email = user_data.get('email') or ''

        # Check if customer has an existing open conversation
        existing_convos = users.document(user_id).collection('conversations') \
            .where('closed', '==', False) \
            .order_by('created_at', direction='DESCENDING') \
            .limit(1) \
            .get()

        if existing_convos:
            conv_id = existing_convos[0].id
            conv_ref = users.document(user_id).collection('conversations').document(conv_id)
            conv_ref.update({
                'escalated': True,
                'escalated_at': now,
                'escalated_by': 'admin',
                'last_updated': now
            })
        else:
            # Create new conversation
            conv_id = f'conv_{int(now.timestamp() * 1000)}'
            conv_ref = users.document(user_id).collection('conversations').document(conv_id)
            conv_ref.set({
                'created_at': now,
                'last_updated': now,
                'escalated': True,
                'escalated_at': now,
                'escalated_by': 'admin',
                'closed': False
            })

        # Save admin message
        order_context = data.get('order_context')
        conv_ref.collection('messages').add({
            'text': message,
            'sender': 'admin',
            'timestamp': now,
            'created_at': now,
            'order_context': order_context
        })

        # Update root conversations collection (shows in admin conversations page)
        conversations.document(conv_id).set({
            'user_id': user_id,
            'conversation_id': conv_id,
            'customer_name': customer_name,
            'email': customer_email,
            'last_message': message[:50],
            'last_updated': now,
            'escalated': True,
            'unread': False
        }, merge=True)

        # Write notification for customer (triggers notification.js onSnapshot)
        db.collection('notifications').add({
            'user_id': user_id,
            'title': '💬 Message from Mrs. Brave\'s',
            'message': message[:80],
            'is_read': False,
            'created_at': now,
            'order_id': None
        })

        return jsonify({'success': True, 'conversation_id': conv_id})

    except Exception:
        app.logger.exception("Error in admin_initiate_conversation")
        return jsonify({'success': False, 'error': 'Internal server error'}), 500    
    
# ---------------- CUSTOMER RESET/START NEW CONVERSATION ----------------
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
                    convo_data = convo_doc.to_dict()  # define conv_data here
                    
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
                        "last_time_dt": ts or None, 
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
    
@app.route('/consultation', methods=['POST'])
@login_required
@limiter.limit("5 per minute")
def consultation():
    user_id = session.get('user_id')
    now = datetime.now(PH_TZ)

    # Recompute price server-side   
    # Handle inspo image upload
    file = request.files.get('image')
    if file and file.filename:
        inspo_image = save_uploaded_image(file, 'consultation')
        if inspo_image is None:
            flash('Image too large or invalid! Max 2MB.', 'danger')
            return redirect(url_for('customize'))
    else:
        inspo_image = None
    try:
        icing_key   = request.form.get('design', '').split('|')[0]
        size_key    = request.form.get('cakeSize', '').split('|')[0]
        layers_key  = request.form.get('layers', '').split('|')[0]
        toppers_key = request.form.get('toppers', '').split('|')[0]

        prices = get_custom_prices()
        amount = 0.0
        amount += float(prices['icing'].get(icing_key, 0))
        amount += float(prices['size'].get(size_key, 0))
        amount += float(prices['layers'].get(layers_key, 0))
        amount += float(prices['toppers'].get(toppers_key, 0))
        for addon_key in ['filling','cupcake','ediblepaper','fondanttoppers','sprinkles','drip','flowers']:
            if request.form.get(addon_key):
                amount += float(prices['addons'].get(addon_key, 0))
        if request.form.get('rush') == 'yes':
            amount += 300
    except Exception:
        app.logger.exception('Error computing consultation price')
        flash('Unable to process. Please try again.', 'danger')
        return redirect(url_for('customize'))

    order_item = request.form.get('order_item', '')

    # Get or create conversation ID
    conv_snapshot = users.document(user_id).collection('conversations') \
        .order_by('created_at', direction=firestore.Query.DESCENDING).limit(1).get()

    if conv_snapshot:
        conv_id = conv_snapshot[0].id
        conv_data = conv_snapshot[0].to_dict()
        # If already escalated/active, create new one
        if conv_data.get('closed') or conv_data.get('is_consultation'):
            conv_id = f'conv_{int(now.timestamp() * 1000)}_{secrets.token_hex(4)}'
    else:
        conv_id = f'conv_{int(now.timestamp() * 1000)}_{secrets.token_hex(4)}'

    consultation_data = {
        'order_item': order_item,
        'amount':     round(amount, 2),
        'cakeType':   request.form.get('cakeType', ''),
        'cakeShape':  request.form.get('cakeShape', ''),
        'cakeSize':   request.form.get('cakeSize', ''),
        'design':     request.form.get('design', ''),
        'layers':     request.form.get('layers', ''),
        'toppers':    request.form.get('toppers', ''),
        'candles':    request.form.get('candles', ''),
        'notes':      request.form.get('notes', '')[:500],
        'inspo_image': inspo_image,
        'filling':        request.form.get('filling', ''),
        'cupcake':        request.form.get('cupcake', ''),
        'ediblepaper':    request.form.get('ediblepaper', ''),
        'fondanttoppers': request.form.get('fondanttoppers', ''),
        'sprinkles':      request.form.get('sprinkles', ''),
        'drip':           request.form.get('drip', ''),
        'flowers':        request.form.get('flowers', ''),
        'rush': request.form.get('rush', ''),
    }

    conv_ref = users.document(user_id).collection('conversations').document(conv_id)
    conv_ref.set({
        'created_at':        now,
        'last_updated':      now,
        'escalated':         True,
        'escalated_at':      now,
        'escalated_by':      'customer',
        'is_consultation':   True,
        'consultation_data': consultation_data,
        'closed':            False,
    }, merge=True)

    # Bot message
    conv_ref.collection('messages').add({
        'text':       f'📋 Consultation request sent! Design: {order_item}. Estimated price: ₱{amount:,.2f}. The owner will reply shortly.',
        'sender':     'bot',
        'timestamp':  now,
        'created_at': now,
    })

    # Top-level conversations mirror (for admin dashboard)
    user_doc = users.document(user_id).get().to_dict() or {}
    conversations.document(conv_id).set({
        'user_id':           user_id,
        'conversation_id':   conv_id,
        'customer_name':     user_doc.get('fname') or user_doc.get('username') or 'Customer',
        'email':             user_doc.get('email', ''),
        'last_message':      f'[Consultation] {order_item[:50]}',
        'last_updated':      now,
        'escalated':         True,
        'is_consultation':   True,
        'consultation_data': consultation_data,
        'unread':            True,
    }, merge=True)

    return redirect(url_for('consultation_sent'))

@app.route('/admin/consultation-to-order', methods=['POST'])
@admin_required
def consultation_to_order():
    user_id = request.form.get('user_id')
    conv_id = request.form.get('conversation_id')

    conv_ref = users.document(user_id).collection('conversations').document(conv_id)
    conv_doc = conv_ref.get()
    if not conv_doc.exists:
        flash('Consultation not found.', 'danger')
        return redirect(url_for('admin_conversations'))

    data = conv_doc.to_dict()
    cd   = data.get('consultation_data', {})

    return render_template('admin_consultation_review.html',
        user_id     = user_id,
        conv_id     = conv_id,
        cd          = cd,
        customer_name = data.get('customer_name', '') or '',
    )
    
@app.route('/admin/consultation-confirm', methods=['POST'])
@admin_required
def consultation_confirm():
    user_id = request.form.get('user_id')
    conv_id = request.form.get('conv_id')
    now     = datetime.now(PH_TZ)

    conv_ref = users.document(user_id).collection('conversations').document(conv_id)
    conv_doc = conv_ref.get()
    if not conv_doc.exists:
        flash('Consultation not found.', 'danger')
        return redirect(url_for('admin_conversations'))

    old_cd = conv_doc.to_dict().get('consultation_data', {})

    prices = get_custom_prices()
    icing_key   = request.form.get('design', '').split('|')[0]
    size_key    = request.form.get('cakeSize', '').split('|')[0]
    layers_key  = request.form.get('layers', '').split('|')[0]
    toppers_key = request.form.get('toppers', '').split('|')[0]

    amount = 0.0
    amount += float(prices['icing'].get(icing_key, 0))
    amount += float(prices['size'].get(size_key, 0))
    amount += float(prices['layers'].get(layers_key, 0))
    amount += float(prices['toppers'].get(toppers_key, 0))
    for addon_key in ['filling','cupcake','ediblepaper','fondanttoppers','sprinkles','drip','flowers']:
        if request.form.get(addon_key):
            amount += float(prices['addons'].get(addon_key, 0))
    if old_cd.get('rush') == 'yes':
        amount += 300
    cd = {
        'cakeType':       request.form.get('cakeType', old_cd.get('cakeType', '')),
        'cakeShape':      request.form.get('cakeShape', old_cd.get('cakeShape', '')),
        'cakeSize':       request.form.get('cakeSize', old_cd.get('cakeSize', '')),
        'design':         request.form.get('design', old_cd.get('design', '')),
        'layers':         request.form.get('layers', old_cd.get('layers', '')),
        'toppers':        request.form.get('toppers', old_cd.get('toppers', '')),
        'candles':        request.form.get('candles', old_cd.get('candles', '')),
        'notes':          request.form.get('notes', old_cd.get('notes', ''))[:500],
        'inspo_image':    old_cd.get('inspo_image'),  # image never changes here
        'filling':        request.form.get('filling', ''),
        'cupcake':        request.form.get('cupcake', ''),
        'ediblepaper':    request.form.get('ediblepaper', ''),
        'fondanttoppers': request.form.get('fondanttoppers', ''),
        'sprinkles':      request.form.get('sprinkles', ''),
        'drip':           request.form.get('drip', ''),
        'flowers':        request.form.get('flowers', ''),
        'amount':         round(amount, 2),
        'rush': old_cd.get('rush', ''),
        'order_item': (
            f"{request.form.get('cakeType','').split('|')[0].title()}, "
            f"{size_key} inches, "
            f"{layers_key} layer(s), "
            f"{request.form.get('design','').split('|')[0].title()} icing, "
            f"Toppers: {toppers_key}" + (
                ', Add-ons: ' + ', '.join([
                    k for k in ['filling','cupcake','ediblepaper','fondanttoppers','sprinkles','drip','flowers']
                    if request.form.get(k)
                ]) if any(request.form.get(k) for k in ['filling','cupcake','ediblepaper','fondanttoppers','sprinkles','drip','flowers']) else ''
            )
        ),
    }

    # Generate unique token
    token = secrets.token_urlsafe(32)

    # Save pending consultation
    db.collection('pending_consultations').document(token).set({
        'user_id':           user_id,
        'conv_id':           conv_id,
        'consultation_data': cd,
        'created_at':        now,
        'expires_at':        now + timedelta(days=7),
        'used':              False,
    })

    # Mark conversation as converted
    conv_ref.update({'status': 'converted', 'converted_at': now})
    conversations.document(conv_id).update({
        'status': 'converted',
        'converted_at': now,
        'is_consultation': True,
    })

    # Build link
    base_url      = request.host_url.rstrip('/')
    complete_link = f"{base_url}/complete-order/{token}"

    # Send chat message to customer
    conv_ref.collection('messages').add({
        'text':       f"✅ Your consultation has been approved! Please complete your order here: {complete_link}",
        'sender':     'admin',
        'timestamp':  now,
        'created_at': now,
    })
    conv_ref.update({'last_updated': now})
    conversations.document(conv_id).update({
        'last_message': '✅ Consultation approved — order link sent',
        'last_updated': now,
        'unread':       True,
    })
    invalidate_cache("converted_consultations")
    flash('Customer notified with order link!', 'success')
    return redirect(url_for('admin_conversations'))

@app.route('/complete-order/<token>')
@login_required
def complete_order(token):
    now      = datetime.now(PH_TZ)
    doc_ref  = db.collection('pending_consultations').document(token)
    doc      = doc_ref.get()

    if not doc.exists:
        flash('Invalid or expired link.', 'danger')
        return redirect(url_for('customize'))

    data = doc.to_dict()

    # Must belong to logged-in user
    if data.get('user_id') != session.get('user_id'):
        flash('Unauthorized.', 'danger')
        return redirect(url_for('customize'))

    if data.get('used'):
        flash('This link has already been used.', 'warning')
        return redirect(url_for('customer_dashboard'))

    expires = data.get('expires_at')
    if expires and datetime.now(PH_TZ) > expires:
        flash('This link has expired.', 'danger')
        return redirect(url_for('customize'))

    cd            = data.get('consultation_data', {})
    custom_prices = get_custom_prices()

    return render_template('customization.html',
        custom_prices  = custom_prices,
        customer       = users.document(data['user_id']).get().to_dict(),
        prefill        = cd,
        consult_token  = token,
    )

@app.route('/consultation/sent')
@login_required
def consultation_sent():
    return render_template('consultation_sent.html')
    
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