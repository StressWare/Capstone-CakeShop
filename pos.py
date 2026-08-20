from flask import Blueprint, render_template, request, redirect, url_for, session, flash, current_app, jsonify
from datetime import datetime, timedelta, timezone, date
import json
from db import walkin_orders, cakes, db
from extensions import limiter
from decorators import admin_required
from helpers import log_admin_action
import firebase

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
        "synced_at":       now,          # when the SERVER actually recorded it
        "oversold":        False,        # flipped to True below if stock ran out mid-sale
        "oversold_items":  [],
    }

    doc_ref  = walkin_orders.add(order_data)
    order_id = doc_ref[1].id

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

    flash('Order placed successfully! 🎂', 'success')
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
@pos_bp.route('/pos/history')
@admin_required
def pos_history():
    now = datetime.now(PH_TZ)
    today = now.date()

    date_param = request.args.get('date', '')

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

    orders_list = []
    try:
        docs = walkin_orders.where(
            'created_at', '>=', start_dt
        ).where(
            'created_at', '<=', end_dt
        ).order_by('created_at', direction='DESCENDING').stream()

        for doc in docs:
            order = doc.to_dict()
            order['id'] = doc.id

            created_at = order.get('created_at')
            if isinstance(created_at, datetime):
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
                else:
                    created_at = created_at.astimezone(PH_TZ)
            order['created_at'] = created_at

            orders_list.append(order)
    except Exception:
        current_app.logger.exception("Error fetching pos history")

    total_sales = sum(o.get('amount', 0) for o in orders_list)
    total_txn   = len(orders_list)
    total_cash  = sum(o.get('amount', 0) for o in orders_list if o.get('payment_method', '').lower() == 'cash')
    total_gcash = sum(o.get('amount', 0) for o in orders_list if o.get('payment_method', '').lower() == 'gcash')

    return render_template(
        'admin_pos_history.html',
        orders        = orders_list,
        total_sales   = total_sales,
        total_txn     = total_txn,
        total_cash    = total_cash,
        total_gcash   = total_gcash,
        selected_date = selected_date,
        now           = now.strftime('%B %d, %Y %I:%M %p')
    )