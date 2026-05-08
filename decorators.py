from functools import wraps
from flask import session, redirect, url_for, render_template, flash

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('user_id'):
            flash("Please log in to continue.", "warning")
            return redirect(url_for('auth_page'))
        return f(*args, **kwargs)
    return decorated_function

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
