import time
from datetime import datetime, timedelta
from functools import wraps

from flask import (session, redirect, url_for, render_template, flash,
                   request, jsonify, g)

from helpers import PH_TZ
from utils import get_cache, set_cache, invalidate_cache

# ---------------- CONFIG ----------------
AUTH_CACHE_TTL = 300              # customers: re-check Firebase every 5 min
PRIVILEGED_CACHE_TTL = 60         # admin/professor/developer: every 60 s

PRIVILEGED_CUTOFF_HOUR = 4        # 04:00 Manila
MIN_PRIVILEGED_HOURS = 6          # a login always lasts at least this long
MAX_PRIVILEGED_HOURS = 20         # ...and never longer than this

REVOCATION_SKEW_SECONDS = 5       # tolerate clock drift vs Google
PRIVILEGED_ROLES = ('admin', 'professor', 'developer')
ADMIN_ABSOLUTE_DEADLINE = False

# ---------------- HELPERS ----------------
def _cache_key(uid):
    # same key main.py already invalidates in disable_user / enable_user
    return f"user_disabled_{uid}"


def _privileged_deadline(login_ts: int) -> int:
    """First 04:00 PH time >= login + MIN hours, capped at login + MAX hours."""
    login = datetime.fromtimestamp(login_ts, PH_TZ)
    earliest = login + timedelta(hours=MIN_PRIVILEGED_HOURS)
    cutoff = earliest.replace(hour=PRIVILEGED_CUTOFF_HOUR, minute=0,
                              second=0, microsecond=0)
    if cutoff < earliest:
        cutoff += timedelta(days=1)
    hard_cap = login + timedelta(hours=MAX_PRIVILEGED_HOURS)
    return int(min(cutoff, hard_cap).timestamp())


def is_api_request() -> bool:
    """True for script (fetch) calls that can't follow a login redirect."""
    if request.headers.get('X-Requested-With') in ('fetch', 'XMLHttpRequest'):
        return True
    return request.accept_mimetypes.best == 'application/json'


def auth_failure(message, category="warning", status=401):
    """Pages -> flash + redirect. Fetch calls -> JSON."""
    if is_api_request():
        return jsonify({
            'error': 'session_expired' if status == 401 else 'forbidden',
            'message': message,
            'login_url': url_for('auth_page'),
        }), status
    flash(message, category)
    return redirect(url_for('auth_page'))


def _forbidden():
    if is_api_request():
        return auth_failure("You do not have permission to access this page.",
                            "danger", 403)
    return render_template('403.html'), 403


def _expired_response():
    return auth_failure("Your session has expired. Please log in again.", "warning")


def start_session(uid, user_data, username=None):
    """ONE entry point for both login paths. Returns the deadline (epoch s) or None."""
    now = int(time.time())
    privileged = any(bool(user_data.get(r)) for r in PRIVILEGED_ROLES)

    session['user_id'] = uid
    session['user'] = user_data
    if username is not None:
        session['username'] = username
    session['login_at'] = now
    session['privileged'] = privileged
    session['session_deadline'] = _privileged_deadline(now) if (privileged and ADMIN_ABSOLUTE_DEADLINE) else None
    session.permanent = True

    invalidate_cache(_cache_key(uid))   # next request sees fresh claims
    return session['session_deadline']


def revoke_user_sessions(uid):
    """Kill all refresh tokens; Flask cookies issued before now die too."""
    from firebase_admin import auth
    auth.revoke_refresh_tokens(uid)
    invalidate_cache(_cache_key(uid))


def enforce_session_policy():
    """
    app.before_request hook. Cookie-only (no Firebase call). Past-deadline
    privileged sessions are cleared and the request continues as anonymous;
    protected routes then answer through auth_failure().
    """
    if not session.get('user_id'):
        return None
    if request.endpoint == 'static':
        return None

    if session.get('privileged') and ADMIN_ABSOLUTE_DEADLINE:
        deadline = session.get('session_deadline')
        if not deadline or time.time() >= deadline:
            session.clear()
            g.session_expired = True
    elif not session.get('privileged') and any(bool((session.get('user') or {}).get(r)) for r in PRIVILEGED_ROLES):
        # privileged cookie from before this feature -> force re-login
        session.clear()
        g.session_expired = True
    return None


def _load_account(uid, privileged):
    """Cached Firebase lookup -> dict, or None if Firebase failed."""
    from firebase_admin import auth

    ttl = PRIVILEGED_CACHE_TTL if privileged else AUTH_CACHE_TTL
    info = get_cache(_cache_key(uid), ttl=ttl)
    if isinstance(info, dict):          # old bool entries are ignored/refreshed
        return info
    try:
        fb = auth.get_user(uid)
    except Exception:
        return None
    info = {
        'disabled': bool(fb.disabled),
        'claims': dict(fb.custom_claims or {}),
        'valid_after': int((fb.tokens_valid_after_timestamp or 0) / 1000),  # ms -> s
    }
    set_cache(_cache_key(uid), info)
    return info


def _check_account(require=None):
    """Shared gate. None = OK, otherwise a response. require: None|'admin'|'staff'."""
    if getattr(g, 'session_expired', False):
        return _expired_response()

    uid = session.get('user_id')
    if not uid:
        return auth_failure("Please log in to continue.", "warning")

    was_privileged = bool(session.get('privileged'))
    info = _load_account(uid, was_privileged)
    if info is None:                    # fail closed, like the old login_required
        session.clear()
        return _expired_response()

    if info['disabled']:
        session.clear()
        invalidate_cache(_cache_key(uid))
        return auth_failure("Your account has been disabled. Contact support.", "danger")

    # refresh tokens revoked after this login -> this cookie is dead too
    login_at = int(session.get('login_at') or 0)
    if login_at + REVOCATION_SKEW_SECONDS < info['valid_after']:
        session.clear()
        return _expired_response()

    claims = info['claims']
    live_privileged = any(bool(claims.get(r)) for r in PRIVILEGED_ROLES)

    # promoted since login: cookie has no deadline -> log in again
    if live_privileged and not was_privileged:
        session.clear()
        return _expired_response()

    if was_privileged and ADMIN_ABSOLUTE_DEADLINE:
        deadline = session.get('session_deadline')
        if not deadline or time.time() >= deadline:
            session.clear()
            return _expired_response()

    if require == 'admin' and not claims.get('admin'):
        return _forbidden()
    if require == 'staff' and not (claims.get('professor') or claims.get('developer')):
        return _forbidden()
    return None


# ---------------- LOGIN REQUIRED ----------------
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        failure = _check_account()
        if failure is not None:
            return failure
        return f(*args, **kwargs)
    return decorated_function


# ---------------- ADMIN REQUIRED ----------------
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        failure = _check_account(require='admin')
        if failure is not None:
            return failure
        return f(*args, **kwargs)
    return decorated_function


# ---------------- PROF REQUIRED ----------------
def professor_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        failure = _check_account(require='staff')
        if failure is not None:
            return failure
        return f(*args, **kwargs)
    return decorated_function
# ---------------- PROFILE COMPLETION REQUIRED ----------------
def profile_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):

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