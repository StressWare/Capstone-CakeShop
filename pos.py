from flask import Blueprint, render_template, request, redirect, url_for, session, flash, current_app, jsonify, abort
from datetime import datetime, timedelta, timezone, date
import json
import math
import re
from db import walkin_orders, cakes, db, pos_sessions, pos_drafts
from extensions import limiter
from decorators import admin_required
from helpers import log_admin_action
import firebase
from werkzeug.exceptions import HTTPException

pos_bp = Blueprint('pos', __name__)
PH_TZ = timezone(timedelta(hours=8))

# ---------------- POS PAGE ----------------
@pos_bp.route('/pos')
@admin_required
def pos_page():
    available_cakes = []
    for cake_doc in cakes.where("status", "==", True).stream():
        cake_data = cake_doc.to_dict()
        cake_data['id'] = cake_doc.id
        available_cakes.append(cake_data)

    return render_template('admin_pos.html', cakes=available_cakes)


# ---------------- POS PLACE ORDER ----------------
@pos_bp.route('/pos/order', methods=['POST'])
@admin_required
@limiter.limit("30 per minute")
def pos_order():
    now = datetime.now(PH_TZ)

    # Accept both classic form POST (offline fallback / no-JS) and the new
    # fetch()-based JSON POST from the offline queue. Support both transparently.
    is_json_request = request.is_json
    payload = request.get_json(silent=True) if is_json_request else request.form

    if is_json_request:
        items = payload.get('items', [])
    else:
        items_json = payload.get('items', '[]')
        items = json.loads(items_json)

    payment_method = payload.get('payment_method', 'Cash')
    cash_received  = float(payload.get('cash_received', 0) or 0)
    amount         = float(payload.get('amount', 0) or 0)
    change         = cash_received - amount if payment_method == 'Cash' else 0

    order_type      = payload.get('order_type', 'Dine In')
    discount_type   = payload.get('discount_type', 'none')
    discount_amount = float(payload.get('discount_amount', 0) or 0)

    # ── NEW: idempotency key, generated client-side at checkout time ──
    idempotency_key = payload.get('idempotency_key')

    if not items:
        if is_json_request:
            return jsonify({"error": "No items selected"}), 400
        flash('No items selected!', 'warning')
        return redirect(url_for('pos.pos_page'))

    # ── NEW: idempotency check — if this exact order already synced, don't redo it ──
    if idempotency_key:
        existing = walkin_orders.where("idempotency_key", "==", idempotency_key).limit(1).stream()
        existing_doc = next(existing, None)
        if existing_doc:
            order_id = existing_doc.id
            existing_data = existing_doc.to_dict()
            if is_json_request:
                return jsonify({
                    "status": "duplicate_ignored",
                    "order_id": order_id,
                    "oversold": existing_data.get("oversold", False),
                    "receipt_url": url_for('pos.pos_receipt', order_id=order_id),
                }), 200
            flash('Order already recorded.', 'info')
            return redirect(url_for('pos.pos_receipt', order_id=order_id))

    # ── Session attribution: the client stamps its session id at checkout time, so an order
    # queued offline still lands in the right session when it syncs later. The device creates
    # the session itself, so an order can reach the server BEFORE its session has synced:
    # keep the link (flagged) instead of dropping it. Never let a lookup problem block a sale.
    session_id = payload.get('session_id')
    if not (isinstance(session_id, str) and _SESSION_ID_RE.match(session_id)):
        session_id = None
    late_for_session = False
    session_unverified = False
    if session_id:
        try:
            session_snap = pos_sessions.document(session_id).get()
            if not session_snap.exists:
                session_unverified = True
            elif (session_snap.to_dict() or {}).get("status") == "ended":
                # Session ended before this queued order synced: the ended session's totals
                # are an immutable snapshot, so flag the order instead of altering it.
                late_for_session = True
        except Exception:
            current_app.logger.exception("Session lookup failed for POS order")

    item_names = ", ".join([
        f"{i['cake_name']} x{i.get('quantity', 1)} (₱{float(i['price']):.0f})"
        for i in items
    ])

    order_data = {
        "order_items":     items,
        "item":            item_names,
        "amount":          amount,
        "payment_method":  payment_method,
        "cash_received":   cash_received,
        "change":          change,
        "order_source":    "walk-in",
        "cashier_id":      session.get('user_id'),
        "status":          "Completed",
        "created_at":      now,
        "order_type":      order_type,
        "discount_type":   discount_type,
        "discount_amount": discount_amount,
        # ── NEW ──
        "idempotency_key": idempotency_key,
        "session_id":         session_id,
        "session_unverified": session_unverified,
        "late_for_session":   late_for_session,
        "synced_at":       now,          # when the SERVER actually recorded it
        "oversold":        False,        # flipped to True below if stock ran out mid-sale
        "oversold_items":  [],
    }

    doc_ref  = walkin_orders.add(order_data)
    order_id = doc_ref[1].id

    # If this order was resumed from a draft, the draft is finished: remove it.
    # Best effort — a failure here must never affect the sale.
    draft_id = payload.get('draft_id')
    if isinstance(draft_id, str) and _DRAFT_ID_RE.match(draft_id):
        try:
            pos_drafts.document(draft_id).delete()
        except Exception:
            current_app.logger.exception("Could not remove POS draft %s after checkout", draft_id)

    # ── NEW: atomic, transactional stock deduction (replaces read-then-write) ──
    oversold_items = []

    @firebase.firestore.transactional
    def deduct_stock(transaction, cake_ref, ordered_qty, cake_name):
        snapshot = cake_ref.get(transaction=transaction)
        current_qty = snapshot.get("quantity") if snapshot.exists else 0
        current_qty = current_qty or 0
        new_qty = current_qty - ordered_qty

        if new_qty < 0:
            oversold_items.append({
                "cake_id": cake_ref.id,
                "cake_name": cake_name,
                "requested": ordered_qty,
                "actually_available": current_qty,
                "oversold_by": abs(new_qty),
            })
            new_qty = 0  # never let stock go negative in the DB

        transaction.update(cake_ref, {
            "quantity": new_qty,
            "status": new_qty > 0,
        })

    transaction = db.transaction()
    for i in items:
        cake_ref = cakes.document(i["cake_id"])
        ordered_qty = int(i.get("quantity", 1))
        deduct_stock(transaction, cake_ref, ordered_qty, i.get("cake_name", ""))

    if oversold_items:
        walkin_orders.document(order_id).update({
            "oversold": True,
            "oversold_items": oversold_items,
        })
        log_admin_action(
            action="POS order OVERSOLD stock",
            target=f"Order {order_id} — {oversold_items}",
            category="pos"
        )

    if late_for_session:
        log_admin_action(
            action="POS order synced after its session was ended",
            target=f"Order {order_id} — session {session_id} (not in that session's closing totals)",
            category="pos"
        )

    log_admin_action(
        action="Created POS order",
        target=f"Walk-in order {order_id} — {item_names}",
        category="pos"
    )

    if is_json_request:
        return jsonify({
            "status": "ok",
            "order_id": order_id,
            "oversold": bool(oversold_items),
            "oversold_items": oversold_items,
            "receipt_url": url_for('pos.pos_receipt', order_id=order_id),
        }), 200

    flash('Order placed successfully!', 'success')
    return redirect(url_for('pos.pos_receipt', order_id=order_id))


# ---------------- POS RECEIPT ----------------
@pos_bp.route('/pos/receipt/<order_id>')
@admin_required
def pos_receipt(order_id):
    order_doc = walkin_orders.document(order_id).get()
    if not order_doc.exists:
        flash('Receipt not found!', 'danger')
        return redirect(url_for('pos.pos_page'))

    order = order_doc.to_dict()
    order['id'] = order_id

    created_at = order.get('created_at')
    if isinstance(created_at, datetime):
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
        else:
            created_at = created_at.astimezone(PH_TZ)
    order['created_at'] = created_at

    return render_template('admin_pos_receipt.html', order=order)


# ---------------- POS HISTORY ----------------
def _history_range(date_param):
    """Parse the ?date= filter into (start_dt, end_dt, label, now). Same rules as before."""
    now = datetime.now(PH_TZ)
    today = now.date()

    if date_param.startswith('week_'):
        start_str = date_param.replace('week_', '')
        try:
            start_date = datetime.strptime(start_str, '%Y-%m-%d').date()
        except Exception:
            start_date = today - timedelta(days=today.weekday())
        end_date = today
        selected_date = f"Week of {start_date.strftime('%b %d')} – {end_date.strftime('%b %d, %Y')}"
    elif date_param:
        try:
            start_date = datetime.strptime(date_param, '%Y-%m-%d').date()
            end_date = start_date
            selected_date = start_date.strftime('%B %d, %Y')
        except Exception:
            start_date = today
            end_date = today
            selected_date = today.strftime('%B %d, %Y')
    else:
        start_date = today
        end_date = today
        selected_date = today.strftime('%B %d, %Y')

    start_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=PH_TZ)
    end_dt   = datetime.combine(end_date, datetime.max.time()).replace(tzinfo=PH_TZ)
    return start_dt, end_dt, selected_date, now


def _normalize_order(doc):
    order = doc.to_dict() or {}
    order['id'] = doc.id
    created_at = order.get('created_at')
    if isinstance(created_at, datetime):
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
        else:
            created_at = created_at.astimezone(PH_TZ)
    order['created_at'] = created_at
    return order


def _fetch_orders(start_dt, end_dt):
    docs = walkin_orders.where(
        'created_at', '>=', start_dt
    ).where(
        'created_at', '<=', end_dt
    ).order_by('created_at', direction='DESCENDING').stream()
    return [_normalize_order(doc) for doc in docs]


def _order_totals(orders):
    cash = sum(o.get('amount', 0) for o in orders if str(o.get('payment_method', '')).lower() == 'cash')
    gcash = sum(o.get('amount', 0) for o in orders if str(o.get('payment_method', '')).lower() == 'gcash')
    return {
        "total_sales": sum(o.get('amount', 0) for o in orders),
        "total_txn":   len(orders),
        "total_cash":  cash,
        "total_gcash": gcash,
    }


@pos_bp.route('/pos/history')
@admin_required
def pos_history():
    """History is organised by SESSION (who ran the till). Click a session to see its orders."""
    date_param = request.args.get('date', '')
    start_dt, end_dt, selected_date, now = _history_range(date_param)

    orders_list = []
    try:
        orders_list = _fetch_orders(start_dt, end_dt)
    except Exception:
        current_app.logger.exception("Error fetching pos history")

    sessions_list, unassigned = [], None
    try:
        sessions_list, unassigned = _history_sessions(start_dt, end_dt, orders_list)
    except Exception:
        current_app.logger.exception("Error fetching POS cash sessions for history")

    return render_template(
        'admin_pos_history.html',
        sessions      = sessions_list,
        unassigned    = unassigned,
        date_param    = date_param or None,
        selected_date = selected_date,
        now           = now.strftime('%B %d, %Y %I:%M %p'),
        **_order_totals(orders_list)
    )


@pos_bp.route('/pos/history/session/<session_id>')
@admin_required
def pos_history_session(session_id):
    """One session's orders (same table as the old history page), or 'unassigned' orders."""
    date_param = request.args.get('date', '')
    back_url = url_for('pos.pos_history', date=date_param or None)
    is_unassigned = (session_id == 'unassigned')
    if not is_unassigned and not _SESSION_ID_RE.match(session_id):
        abort(404)

    info = None
    orders_list = []
    try:
        if is_unassigned:
            start_dt, end_dt, selected_date, now = _history_range(date_param)
            all_orders = _fetch_orders(start_dt, end_dt)
            orders_list, _ = _split_unassigned(all_orders, {})
            page_title, page_sub = 'Unassigned orders', selected_date
        else:
            now = datetime.now(PH_TZ)
            snap = pos_sessions.document(session_id).get()
            if not snap.exists:
                return abort(404)
            data = snap.to_dict() or {}
            orders_list = sorted(
                (_normalize_order(d) for d in walkin_orders.where('session_id', '==', session_id).stream()),
                key=lambda o: o.get('created_at') or datetime.min.replace(tzinfo=PH_TZ), reverse=True)
            movements = _session_movements(session_id)
            info = _session_info(data, orders_list, movements)
            page_title = info['name']
            page_sub = info['started_label'] + (f" → {info['ended_label']}" if info['ended_label'] else '')
    except HTTPException:
        raise                                       # the 404 above
    except Exception:
        current_app.logger.exception("Error loading POS session history")
        flash("Could not load this session. Please try again.", "danger")
        return redirect(back_url)

    pdf_name = "MrsBraves_POS_" + re.sub(r'[^A-Za-z0-9]+', '_', f"{page_title} {page_sub}").strip('_')
    return render_template(
        'admin_pos_session.html',
        page_title   = page_title,
        page_sub     = page_sub,
        back_url     = back_url,
        orders       = orders_list,
        session_info = info,
        pdf_name     = pdf_name,
        now          = now.strftime('%B %d, %Y %I:%M %p'),
        **_order_totals(orders_list)
    )


# =====================================================================
# CASH SESSIONS  ("Start of day" -> "End day")
# The DEVICE creates the session (client-generated id), so a session can be
# started, take Pay in/out and be ended while the internet is down, then sync.
# Expected drawer cash = starting_cash + cash_sales + paid_in - paid_out.
# GCash is confirmed physically by the owner, so it is reported but is NOT
# part of drawer cash.
# =====================================================================
MAX_CASH_AMOUNT = 1_000_000
MAX_NAME_LEN    = 60
MAX_COMMENT_LEN = 200
MAX_NOTE_LEN    = 500
_SESSION_ID_RE  = re.compile(r'^[A-Za-z0-9]{8,64}$')
_REQUEST_ID_RE  = re.compile(r'^(?!__)[A-Za-z0-9_-]{8,64}$')   # client-generated idempotency id

# Server-side sanity window for client-supplied times. The POS form enforces the strict
# "not in the future / at most 7 days back" rule; the server stays loose on purpose so
# device clock skew or a long outage can never make a legitimate session unsyncable.
_MAX_FUTURE = timedelta(days=1)
_MAX_PAST   = timedelta(days=400)


class SessionError(Exception):
    """Expected, user-facing failure (validation / state conflict)."""
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------- session helpers: validation ----------------
def _money(value):
    return round(float(value or 0), 2)


def _parse_amount(value, field, allow_zero=False):
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        raise SessionError(f"{field} is required")
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise SessionError(f"{field} must be a number")
    if not math.isfinite(amount):
        raise SessionError(f"{field} must be a number")
    amount = round(amount, 2)
    if amount < 0 or (amount == 0 and not allow_zero):
        raise SessionError(f"{field} must be greater than zero" if not allow_zero
                           else f"{field} cannot be negative")
    if amount > MAX_CASH_AMOUNT:
        raise SessionError(f"{field} is too large")
    return amount


def _clean_text(value, field, max_len, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise SessionError(f"{field} must be text")
    value = value.strip()
    if required and not value:
        raise SessionError(f"{field} is required")
    if len(value) > max_len:
        raise SessionError(f"{field} must be at most {max_len} characters")
    if any(ord(ch) < 32 for ch in value):
        raise SessionError(f"{field} contains invalid characters")
    return value


def _parse_client_time(value, field):
    if not isinstance(value, str) or not value.strip():
        raise SessionError(f"{field} is required")
    text = value.strip()
    if text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise SessionError(f"{field} is not a valid date/time")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=PH_TZ)
    now = datetime.now(PH_TZ)
    if dt > now + _MAX_FUTURE or dt < now - _MAX_PAST:
        raise SessionError(f"{field} is out of range")
    return dt.astimezone(PH_TZ)


def _json_body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise SessionError("Invalid JSON body")
    return body


def _body_session_id(body):
    session_id = body.get("session_id")
    if not (isinstance(session_id, str) and _SESSION_ID_RE.match(session_id)):
        raise SessionError("A valid session_id is required")
    return session_id


def _admin_identity():
    user = session.get("user") or {}
    return user.get("name") or "admin", session.get("user_id")


# ---------------- session helpers: serialization ----------------
def _as_ph(dt):
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(PH_TZ)
    return None


def _iso(dt):
    dt = _as_ph(dt)
    return dt.isoformat() if dt else None


def _serialize_session(session_id, data, totals=None, movements=None):
    out = {
        "id":            session_id,
        "name":          data.get("name"),
        "status":        data.get("status"),
        "started_at":    _iso(data.get("started_at")),
        "starting_cash": _money(data.get("starting_cash")),
    }
    if data.get("status") == "ended":
        out.update({
            "ended_at":         _iso(data.get("ended_at")),
            "counted_cash":     _money(data.get("counted_cash")),
            "expected_cash":    _money(data.get("expected_cash")),
            "difference":       _money(data.get("difference")),
            "local_expected":   data.get("local_expected"),
            "local_difference": data.get("local_difference"),
            "note":             data.get("note", ""),
        })
    if totals is not None:
        out["totals"] = totals
    if movements is not None:
        out["movements"] = movements
    return out


def _json(payload, status=200):
    resp = jsonify(payload)
    resp.status_code = status
    resp.headers["Cache-Control"] = "no-store"   # never let a browser/SW serve stale cash totals
    return resp


# ---------------- session helpers: Firestore ----------------
def _compute_session_totals(session_id, starting_cash, transaction=None):
    """Aggregate a session's orders + cash movements. Returns (totals, movements)."""
    cash_sales = gcash_sales = other_sales = discounts = 0.0
    txn_count = 0
    for doc in walkin_orders.where("session_id", "==", session_id).stream(transaction=transaction):
        o = doc.to_dict() or {}
        amount = float(o.get("amount") or 0)          # post-discount total actually charged
        method = str(o.get("payment_method", "")).lower()
        if method == "cash":
            cash_sales += amount
        elif method == "gcash":
            gcash_sales += amount
        else:
            other_sales += amount
        discounts += float(o.get("discount_amount") or 0)
        txn_count += 1

    paid_in = paid_out = 0.0
    movements = []
    mv_query = pos_sessions.document(session_id).collection("cash_movements").order_by("created_at")
    for doc in mv_query.stream(transaction=transaction):
        m = doc.to_dict() or {}
        amount = float(m.get("amount") or 0)
        if m.get("type") == "paid_in":
            paid_in += amount
        elif m.get("type") == "paid_out":
            paid_out += amount
        movements.append({
            "id":         doc.id,
            "type":       m.get("type"),
            "amount":     _money(amount),
            "comment":    m.get("comment", ""),
            "created_at": _iso(m.get("client_at") or m.get("created_at")),
            "created_by": m.get("created_by"),
        })

    net_sales = cash_sales + gcash_sales + other_sales
    totals = {
        "transactions":  txn_count,
        "cash_sales":    _money(cash_sales),
        "gcash_sales":   _money(gcash_sales),
        "other_sales":   _money(other_sales),
        "discounts":     _money(discounts),
        "gross_sales":   _money(net_sales + discounts),
        "net_sales":     _money(net_sales),
        "paid_in":       _money(paid_in),
        "paid_out":      _money(paid_out),
        "expected_cash": _money(float(starting_cash or 0) + cash_sales + paid_in - paid_out),
    }
    return totals, movements


def _start_session(session_id, name, started_at, starting_cash, admin_name, admin_id):
    """Create-only and idempotent: re-sending the same Start is a harmless no-op."""
    ref = pos_sessions.document(session_id)
    now = datetime.now(PH_TZ)

    @firebase.firestore.transactional
    def _txn(transaction):
        if ref.get(transaction=transaction).exists:
            return False
        transaction.set(ref, {
            "name":          name,
            "status":        "open",
            "started_at":    started_at,      # as typed on the POS (editable, validated)
            "received_at":   now,             # when the server actually got it
            "started_by":    admin_name,      # the logged-in admin account
            "started_by_id": admin_id,
            "starting_cash": starting_cash,
        })
        return True

    return _txn(db.transaction())


def _add_cash_movement(session_id, mtype, amount, comment, client_at, admin_name, admin_id, request_id=None):
    session_ref = pos_sessions.document(session_id)
    movements_col = session_ref.collection("cash_movements")
    # A client-generated request_id doubles as the doc id, so a retry after a flaky
    # connection can never record the same Pay in / Pay out twice.
    mv_ref = movements_col.document(request_id) if request_id else movements_col.document()
    now = datetime.now(PH_TZ)

    @firebase.firestore.transactional
    def _txn(transaction):
        snap = session_ref.get(transaction=transaction)           # reads first
        if not snap.exists:
            raise SessionError("Session not found", 404)
        already = bool(request_id) and mv_ref.get(transaction=transaction).exists
        if already:
            return False                                          # already recorded — no-op
        if (snap.to_dict() or {}).get("status") != "open":
            raise SessionError("This session has already ended", 409)
        transaction.set(mv_ref, {
            "type":          mtype,
            "amount":        amount,
            "comment":       comment,
            "client_at":     client_at,
            "created_at":    now,
            "created_by":    admin_name,
            "created_by_id": admin_id,
        })
        return True

    created = _txn(db.transaction())
    return mv_ref.id, created


def _end_session(session_id, counted_cash, ended_at, note, local_expected, local_difference,
                 admin_name, admin_id):
    """Idempotent: ending an already-ended session returns it unchanged. Returns (data, ended_now)."""
    ref = pos_sessions.document(session_id)
    now = datetime.now(PH_TZ)

    @firebase.firestore.transactional
    def _txn(transaction):
        snap = ref.get(transaction=transaction)                   # reads first
        if not snap.exists:
            raise SessionError("Session not found", 404)
        data = snap.to_dict() or {}
        if data.get("status") == "ended":
            return data, False
        started = _as_ph(data.get("started_at"))
        if started and ended_at < started:
            raise SessionError("End time cannot be before the start time")
        totals, _ = _compute_session_totals(session_id, data.get("starting_cash"), transaction)
        expected   = totals["expected_cash"]
        difference = _money(counted_cash - expected)              # + over, - short
        update = {
            "status":           "ended",
            "ended_at":         ended_at,
            "ended_received_at": now,
            "ended_by":         admin_name,
            "ended_by_id":      admin_id,
            "counted_cash":     counted_cash,
            "expected_cash":    expected,
            "difference":       difference,
            "local_expected":   local_expected,       # the device's own estimate, kept for comparison
            "local_difference": local_difference,
            "note":             note,
            "summary":          totals,
        }
        transaction.update(ref, update)
        return {**data, **update}, True

    return _txn(db.transaction())


def _optional_number(value):
    """Client estimates are informational only; ignore anything that isn't a sane number."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return round(n, 2) if math.isfinite(n) and abs(n) <= MAX_CASH_AMOUNT * 10 else None


# ---------------- POS history: sessions ----------------
def _diff_display(diff):
    if diff is None:
        return '', ''
    if diff == 0:
        return 'Exact', 'diff-ok'
    if diff > 0:
        return f'Over ₱{diff:,.2f}', 'diff-over'
    return f'Short ₱{-diff:,.2f}', 'diff-short'


def _status_label(data, started):
    if data.get('status') == 'ended':
        return 'Ended'
    today = datetime.now(PH_TZ).date()
    return 'Not ended' if (started and started.date() < today) else 'In progress'


def _valid_session_id(value):
    return isinstance(value, str) and bool(_SESSION_ID_RE.match(value))


def _load_sessions(ids):
    """Fetch session docs by id. Returns {id: data} for the ones that exist."""
    found = {}
    for sid in ids:
        if not _valid_session_id(sid):
            continue
        try:
            snap = pos_sessions.document(sid).get()
            if snap.exists:
                found[sid] = snap.to_dict() or {}
        except Exception:
            current_app.logger.exception("Could not load POS session %s", sid)
    return found


def _split_unassigned(orders, known_sessions):
    """Split orders into (unassigned, assigned). An order is unassigned when it has no
    session id, or points at a session that does not exist (e.g. its Start never synced)."""
    referenced = {o.get('session_id') for o in orders if _valid_session_id(o.get('session_id'))}
    existing = dict(known_sessions)
    existing.update(_load_sessions(referenced - set(known_sessions)))
    unassigned, assigned = [], []
    for o in orders:
        sid = o.get('session_id')
        (assigned if (_valid_session_id(sid) and sid in existing) else unassigned).append(o)
    return unassigned, existing


def _history_sessions(start_dt, end_dt, orders):
    """Sessions to list for a date range: those STARTED in the range, plus any session that has
    orders in the range (e.g. started late last night). Returns (cards, unassigned_summary|None)."""
    known = {}
    docs = pos_sessions.where('started_at', '>=', start_dt).where(
        'started_at', '<=', end_dt).stream()
    for doc in docs:
        known[doc.id] = doc.to_dict() or {}

    unassigned_orders, known = _split_unassigned(orders, known)

    def started_key(item):
        return _as_ph(item[1].get('started_at')) or datetime.min.replace(tzinfo=PH_TZ)

    cards = []
    for sid, d in sorted(known.items(), key=started_key, reverse=True):
        started, ended = _as_ph(d.get('started_at')), _as_ph(d.get('ended_at'))
        counted = diff = None
        if d.get('status') == 'ended':
            t = d.get('summary') or {}
            expected = _money(d.get('expected_cash'))
            counted, diff = _money(d.get('counted_cash')), _money(d.get('difference'))
        else:
            t, _ = _compute_session_totals(sid, d.get('starting_cash'))
            expected = t['expected_cash']
        diff_label, diff_class = _diff_display(diff)
        cards.append({
            "id":            sid,
            "name":          d.get('name') or '—',
            "status_label":  _status_label(d, started),
            "started_label": started.strftime('%b %d, %I:%M %p') if started else '—',
            "ended_label":   ended.strftime('%b %d, %I:%M %p') if ended else None,
            "starting_cash": _money(d.get('starting_cash')),
            "expected_cash": expected,
            "counted_cash":  counted,
            "diff_label":    diff_label,
            "diff_class":    diff_class,
            "cash_sales":    t.get('cash_sales', 0),
            "gcash_sales":   t.get('gcash_sales', 0),
            "paid_in":       t.get('paid_in', 0),
            "paid_out":      t.get('paid_out', 0),
            "transactions":  t.get('transactions', 0),
            "note":          d.get('note', ''),
        })

    unassigned = None
    if unassigned_orders:
        unassigned = {"count": len(unassigned_orders),
                      "total": sum(o.get('amount', 0) for o in unassigned_orders)}
    return cards, unassigned


def _session_movements(session_id):
    out = []
    q = pos_sessions.document(session_id).collection("cash_movements").order_by("created_at")
    for doc in q.stream():
        m = doc.to_dict() or {}
        when = _as_ph(m.get("client_at") or m.get("created_at"))
        out.append({
            "type":    m.get("type"),
            "label":   "Paid in" if m.get("type") == "paid_in" else "Paid out",
            "amount":  _money(m.get("amount")),
            "comment": m.get("comment", ""),
            "time":    when.strftime('%I:%M %p') if when else '',
        })
    return out


def _session_info(data, orders, movements):
    """Everything the session page shows about the session itself."""
    started, ended = _as_ph(data.get('started_at')), _as_ph(data.get('ended_at'))
    starting = _money(data.get('starting_cash'))
    paid_in = sum(m['amount'] for m in movements if m['type'] == 'paid_in')
    paid_out = sum(m['amount'] for m in movements if m['type'] == 'paid_out')
    counted = diff = None
    if data.get('status') == 'ended':
        expected = _money(data.get('expected_cash'))
        counted, diff = _money(data.get('counted_cash')), _money(data.get('difference'))
    else:
        cash_sales = sum(o.get('amount', 0) for o in orders if str(o.get('payment_method', '')).lower() == 'cash')
        expected = _money(starting + cash_sales + paid_in - paid_out)
    diff_label, diff_class = _diff_display(diff)
    return {
        "name":          data.get('name') or '—',
        "status_label":  _status_label(data, started),
        "started_label": started.strftime('%b %d, %Y %I:%M %p') if started else '—',
        "ended_label":   ended.strftime('%b %d, %Y %I:%M %p') if ended else None,
        "starting_cash": starting,
        "expected_cash": expected,
        "counted_cash":  counted,
        "diff_label":    diff_label,
        "diff_class":    diff_class,
        "paid_in":       _money(paid_in),
        "paid_out":      _money(paid_out),
        "note":          data.get('note', ''),
        "movements":     movements,
        "late_count":    sum(1 for o in orders if o.get('late_for_session')),
    }


# ---------------- SESSION ROUTES ----------------
def _session_error_response(err):
    return _json({"error": err.message}, err.status)


@pos_bp.route('/pos/session/<session_id>')
@admin_required
def pos_session_get(session_id):
    """Live (open) or frozen (ended) view of one session, for the POS screens."""
    try:
        if not _SESSION_ID_RE.match(session_id):
            raise SessionError("Invalid session id", 400)
        snap = pos_sessions.document(session_id).get()
        if not snap.exists:
            raise SessionError("Session not found", 404)
        data = snap.to_dict() or {}
        if data.get("status") == "ended":
            return _json({"session": _serialize_session(session_id, data, data.get("summary"))})
        totals, movements = _compute_session_totals(session_id, data.get("starting_cash"))
        return _json({"session": _serialize_session(session_id, data, totals, movements)})
    except SessionError as err:
        return _session_error_response(err)
    except Exception:
        current_app.logger.exception("Error loading POS session")
        return _json({"error": "Could not load the session"}, 500)


@pos_bp.route('/pos/session/start', methods=['POST'])
@admin_required
@limiter.limit("20 per minute")
def pos_session_start():
    try:
        body = _json_body()
        session_id    = _body_session_id(body)
        name          = _clean_text(body.get("name"), "Name", MAX_NAME_LEN, required=True)
        started_at    = _parse_client_time(body.get("started_at"), "Start time")
        starting_cash = _parse_amount(body.get("starting_cash"), "Starting cash", allow_zero=True)
        admin_name, uid = _admin_identity()
        created = _start_session(session_id, name, started_at, starting_cash, admin_name, uid)
        if created:
            log_admin_action(
                action="Started POS cash session",
                target=f"{name} — starting cash ₱{starting_cash:.2f}",
                category="pos"
            )
        return _json({"status": "ok" if created else "duplicate_ignored", "session_id": session_id},
                     201 if created else 200)
    except SessionError as err:
        return _session_error_response(err)
    except Exception:
        current_app.logger.exception("Error starting POS session")
        return _json({"error": "Could not start the session"}, 500)


@pos_bp.route('/pos/session/cash', methods=['POST'])
@admin_required
@limiter.limit("60 per minute")
def pos_session_cash():
    try:
        body = _json_body()
        session_id = _body_session_id(body)
        mtype = body.get("type")
        if mtype not in ("paid_in", "paid_out"):
            raise SessionError("type must be 'paid_in' or 'paid_out'")
        amount  = _parse_amount(body.get("amount"), "Amount")
        comment = _clean_text(body.get("comment"), "Comment", MAX_COMMENT_LEN)
        request_id = body.get("request_id")
        if request_id is not None and not (isinstance(request_id, str) and _REQUEST_ID_RE.match(request_id)):
            raise SessionError("Invalid request_id")
        client_at = _parse_client_time(body.get("at"), "Time") if body.get("at") else None
        admin_name, uid = _admin_identity()
        movement_id, created = _add_cash_movement(
            session_id, mtype, amount, comment, client_at, admin_name, uid, request_id)
        if created:
            log_admin_action(
                action="POS cash paid in" if mtype == "paid_in" else "POS cash paid out",
                target=f"Session {session_id} — ₱{amount:.2f}" + (f" — {comment}" if comment else ""),
                category="pos"
            )
        return _json({"status": "ok" if created else "duplicate_ignored", "movement_id": movement_id},
                     201 if created else 200)
    except SessionError as err:
        return _session_error_response(err)
    except Exception:
        current_app.logger.exception("Error recording POS cash movement")
        return _json({"error": "Could not record the cash movement"}, 500)


@pos_bp.route('/pos/session/end', methods=['POST'])
@admin_required
@limiter.limit("20 per minute")
def pos_session_end():
    try:
        body = _json_body()
        session_id   = _body_session_id(body)
        counted_cash = _parse_amount(body.get("counted_cash"), "Counted cash", allow_zero=True)
        ended_at     = _parse_client_time(body.get("ended_at"), "End time")
        note         = _clean_text(body.get("note"), "Note", MAX_NOTE_LEN)
        admin_name, uid = _admin_identity()
        data, ended_now = _end_session(
            session_id, counted_cash, ended_at, note,
            _optional_number(body.get("local_expected")),
            _optional_number(body.get("local_difference")),
            admin_name, uid)
        if ended_now:
            log_admin_action(
                action="Ended POS cash session",
                target=(f"{data.get('name')} — expected ₱{data['expected_cash']:.2f}, "
                        f"counted ₱{counted_cash:.2f}, difference ₱{data['difference']:.2f}"),
                category="pos"
            )
        return _json({"status": "ok" if ended_now else "already_ended",
                      "session": _serialize_session(session_id, data, data.get("summary"))})
    except SessionError as err:
        return _session_error_response(err)
    except Exception:
        current_app.logger.exception("Error ending POS session")
        return _json({"error": "Could not end the session"}, 500)



# =====================================================================
# DRAFT ORDERS  (hold an order, finish it later)
# Stored in Firestore (`pos_drafts`), so saving a draft needs internet.
# A draft is NOT a sale: no stock is deducted, nothing counts in sales or
# the cash drawer until it is checked out (checkout removes the draft).
# Stock and prices are re-checked on the POS when a draft is resumed.
# =====================================================================
MAX_DRAFTS     = 30
MAX_DRAFT_ITEMS = 50
DRAFT_TTL      = timedelta(hours=24)      # a draft expires 24h after it was last saved
DRAFT_DISCOUNT_RATE = 0.20                # display only — mirrors the POS Senior/PWD discount
_DRAFT_ID_RE   = re.compile(r'^[A-Za-z0-9]{8,64}$')
_CAKE_ID_RE    = re.compile(r'^[^/\s]{1,128}$')
_ORDER_TYPES   = ("Dine In", "Take Out")
_DISCOUNT_TYPES = ("none", "senior", "pwd")


def _clean_draft_items(items):
    if not isinstance(items, list) or not items:
        raise SessionError("A draft needs at least one item")
    if len(items) > MAX_DRAFT_ITEMS:
        raise SessionError(f"A draft can have at most {MAX_DRAFT_ITEMS} items")
    cleaned = []
    for raw in items:
        if not isinstance(raw, dict):
            raise SessionError("Invalid item")
        cake_id = raw.get("cake_id")
        if not (isinstance(cake_id, str) and _CAKE_ID_RE.match(cake_id)):
            raise SessionError("Invalid item id")
        qty = raw.get("quantity")
        if isinstance(qty, bool) or not isinstance(qty, int) or not (1 <= qty <= 999):
            raise SessionError("Item quantity must be a whole number from 1 to 999")
        cleaned.append({
            "cake_id":   cake_id,
            "cake_name": _clean_text(raw.get("cake_name"), "Item name", 100, required=True),
            "price":     _parse_amount(raw.get("price"), "Item price", allow_zero=True),
            "quantity":  qty,
        })
    return cleaned


def _draft_label(name, when):
    return name or ("Draft " + when.strftime('%I:%M %p').lstrip('0'))


def _serialize_draft(draft_id, d):
    created = _as_ph(d.get("created_at")) or datetime.now(PH_TZ)
    updated = _as_ph(d.get("updated_at")) or created
    return {
        "id":              draft_id,
        "name":            d.get("name", ""),
        "label":           _draft_label(d.get("name", ""), created),
        "items":           d.get("items", []),
        "item_count":      sum(i.get("quantity", 0) for i in d.get("items", [])),
        "order_type":      d.get("order_type", "Dine In"),
        "discount_type":   d.get("discount_type", "none"),
        "subtotal":        _money(d.get("subtotal")),
        "discount_amount": _money(d.get("discount_amount")),
        "total":           _money(d.get("total")),
        "by":              d.get("session_name") or d.get("created_by") or "",
        "saved_at":        updated.strftime('%b %d, %I:%M %p'),
    }


@pos_bp.route('/pos/drafts')
@admin_required
def pos_drafts_list():
    try:
        now = datetime.now(PH_TZ)
        live, expired = [], []
        for doc in pos_drafts.stream():
            d = doc.to_dict() or {}
            exp = _as_ph(d.get("expires_at"))
            if exp and exp <= now:
                expired.append(doc.id)
            else:
                live.append((doc.id, d))
        for draft_id in expired[:20]:               # lazy cleanup (a Firestore TTL policy can do this too)
            try:
                pos_drafts.document(draft_id).delete()
            except Exception:
                current_app.logger.exception("Could not remove expired POS draft %s", draft_id)
        live.sort(key=lambda r: _as_ph(r[1].get("updated_at")) or datetime.min.replace(tzinfo=PH_TZ), reverse=True)
        return _json({"drafts": [_serialize_draft(i, d) for i, d in live]})
    except Exception:
        current_app.logger.exception("Error listing POS drafts")
        return _json({"error": "Could not load drafts"}, 500)


@pos_bp.route('/pos/drafts', methods=['POST'])
@admin_required
@limiter.limit("30 per minute")
def pos_drafts_save():
    """Create a draft, or overwrite one when draft_id is given (a resumed draft saved again)."""
    try:
        body = _json_body()
        items = _clean_draft_items(body.get("items"))
        order_type = body.get("order_type", "Dine In")
        discount_type = body.get("discount_type", "none")
        if order_type not in _ORDER_TYPES:
            raise SessionError("Invalid order type")
        if discount_type not in _DISCOUNT_TYPES:
            raise SessionError("Invalid discount type")
        name = _clean_text(body.get("name"), "Name", MAX_NAME_LEN)
        session_name = _clean_text(body.get("session_name"), "Session name", MAX_NAME_LEN)
        session_id = body.get("session_id")
        if session_id is not None and not _valid_session_id(session_id):
            raise SessionError("Invalid session id")
        draft_id = body.get("draft_id")
        if draft_id is not None and not (isinstance(draft_id, str) and _DRAFT_ID_RE.match(draft_id)):
            raise SessionError("Invalid draft id")

        subtotal = _money(sum(i["price"] * i["quantity"] for i in items))
        discount = _money(subtotal * DRAFT_DISCOUNT_RATE) if discount_type in ("senior", "pwd") else 0.0
        admin_name, _ = _admin_identity()
        now = datetime.now(PH_TZ)
        fields = {
            "name": name, "items": items, "order_type": order_type, "discount_type": discount_type,
            "subtotal": subtotal, "discount_amount": discount, "total": _money(subtotal - discount),
            "session_id": session_id, "session_name": session_name,
            "updated_at": now, "expires_at": now + DRAFT_TTL,
        }

        if draft_id:
            ref = pos_drafts.document(draft_id)
            snap = ref.get()
            if not snap.exists:
                raise SessionError("That draft no longer exists (it may have been used or deleted)", 404)
            ref.update(fields)
            data = {**(snap.to_dict() or {}), **fields}
            created = False
        else:
            if len(list(pos_drafts.limit(MAX_DRAFTS).stream())) >= MAX_DRAFTS:
                raise SessionError(f"Too many drafts ({MAX_DRAFTS}). Finish or delete some first.", 409)
            ref = pos_drafts.document()
            data = {**fields, "created_at": now, "created_by": admin_name}
            ref.set(data)
            created = True

        log_admin_action(
            action="Saved POS draft" if created else "Updated POS draft",
            target=f"{_draft_label(name, now)} — ₱{data['total']:.2f}",
            category="pos"
        )
        return _json({"status": "ok", "draft": _serialize_draft(ref.id, data)}, 201 if created else 200)
    except SessionError as err:
        return _session_error_response(err)
    except Exception:
        current_app.logger.exception("Error saving POS draft")
        return _json({"error": "Could not save the draft"}, 500)


@pos_bp.route('/pos/drafts/<draft_id>', methods=['DELETE'])
@admin_required
@limiter.limit("30 per minute")
def pos_drafts_delete(draft_id):
    try:
        if not _DRAFT_ID_RE.match(draft_id):
            raise SessionError("Invalid draft id")
        ref = pos_drafts.document(draft_id)
        snap = ref.get()
        if not snap.exists:
            raise SessionError("Draft not found", 404)
        d = snap.to_dict() or {}
        ref.delete()
        log_admin_action(
            action="Deleted POS draft",
            target=f"{_draft_label(d.get('name', ''), _as_ph(d.get('created_at')) or datetime.now(PH_TZ))} — ₱{_money(d.get('total')):.2f}",
            category="pos"
        )
        return _json({"status": "ok"})
    except SessionError as err:
        return _session_error_response(err)
    except Exception:
        current_app.logger.exception("Error deleting POS draft")
        return _json({"error": "Could not delete the draft"}, 500)
