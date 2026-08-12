from functools import wraps
from flask import session, redirect, url_for, render_template, flash, request
from utils import get_cache, set_cache, invalidate_cache


AUTH_CACHE_TTL = 300
# ---------------- LOGIN REQUIRED ----------------
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from firebase_admin import auth

        user_id = session.get('user_id')
        if not user_id:
            flash("Please log in to continue.", "warning")
            return redirect(url_for('auth_page'))

        cache_key = f"user_disabled_{user_id}"
        disabled = get_cache(cache_key, ttl=AUTH_CACHE_TTL)

        if disabled is None:
            try:
                firebase_user = auth.get_user(user_id)
                disabled = firebase_user.disabled
                set_cache(cache_key, disabled)
            except Exception:
                session.clear()
                return redirect(url_for('auth_page'))

        if disabled:
            session.clear()
            invalidate_cache(cache_key)
            flash("Your account has been disabled. Contact support.", "danger")
            return redirect(url_for('auth_page'))

        return f(*args, **kwargs)
    return decorated_function
# ---------------- ADMIN REQUIRED ----------------
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        current_user = session.get('user')
        if not current_user:
            flash("Please log in as an admin.", "warning")
            return redirect(url_for('auth_page'))
        if not current_user.get('admin'):
            flash("You do not have permission to access this page.", "danger")
            return render_template('403.html'), 403
        return f(*args, **kwargs)
    return decorated_function

# ---------------- PROF REQUIRED ----------------
def professor_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        current_user = session.get('user')
        if not current_user:
            flash("Please log in.", "warning")
            return redirect(url_for('auth_page'))
        if not (current_user.get('professor') or current_user.get('developer')):
            return render_template('403.html'), 403
        return f(*args, **kwargs)
    return decorated_function
# ---------------- PROFILE COMPLETION REQUIRED ----------------
def profile_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # 🔑 LAZY IMPORT: Runs only when route is accessed, AFTER Firebase is initialized
        from db import users
        
        user_id = session.get('user_id')
        if not user_id:
            return redirect(url_for('auth_page'))
        
        # Fetch user data from Firestore
        doc = users.document(user_id).get()
        if not doc.exists:
            session.clear()
            return redirect(url_for('auth_page'))
        
        customer = doc.to_dict()
        
        # Define required fields for a "complete" profile
        required_fields = ['fname', 'username', 'number', 'address']
        is_incomplete = any(not customer.get(field) or customer.get(field).strip() == '' 
                           for field in required_fields)
        
        # If incomplete AND not already on the complete-profile page → redirect
        if is_incomplete and request.endpoint != 'complete_profile':
            flash('Please complete your profile to continue.', 'warning')
            return redirect(url_for('complete_profile'))
        
        return f(*args, **kwargs)
    return decorated_function