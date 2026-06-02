from functools import wraps
from flask import session, redirect, url_for, render_template, flash, request

# ---------------- LOGIN REQUIRED ----------------
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from firebase_admin import auth

        user_id = session.get('user_id')
        if not user_id:
            flash("Please log in to continue.", "warning")
            return redirect(url_for('auth_page'))

        try:
            firebase_user = auth.get_user(user_id)
            if firebase_user.disabled:
                session.clear()
                flash("Your account has been disabled. Contact support.", "danger")
                return redirect(url_for('auth_page'))
        except Exception:
            session.clear()
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