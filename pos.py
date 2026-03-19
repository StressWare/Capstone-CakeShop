from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from datetime import datetime, timedelta, timezone
from db import walkin_orders, cakes

pos_bp = Blueprint('pos', __name__)

PH_TZ = timezone(timedelta(hours=8))

# ---------------- POS PAGE ----------------
@pos_bp.route('/pos')
def pos_page():
    current_user = session.get('user')
    if not current_user:
        return redirect(url_for('auth_page'))
    if not current_user.get('admin'):
        return render_template('403.html'), 403

    # Fetch available cakes
    available_cakes = []
    for cake_doc in cakes.where("status", "==", True).stream():
        cake_data = cake_doc.to_dict()
        cake_data['id'] = cake_doc.id
        available_cakes.append(cake_data)

    return render_template('pos.html', cakes=available_cakes)


# ---------------- POS PLACE ORDER ----------------
@pos_bp.route('/pos/order', methods=['POST'])
def pos_order():
    current_user = session.get('user')
    if not current_user or not current_user.get('admin'):
        return redirect(url_for('auth_page'))

    now = datetime.now(PH_TZ)

    import json
    items_json     = request.form.get('items', '[]')
    items          = json.loads(items_json)
    payment_method = request.form.get('payment_method', 'Cash')
    cash_received  = float(request.form.get('cash_received', 0))
    amount         = float(request.form.get('amount', 0))
    change         = cash_received - amount if payment_method == 'Cash' else 0

    if not items:
        flash('No items selected!', 'warning')
        return redirect(url_for('pos.pos_page'))

    # Build item string for admin dashboard
    item_names = ", ".join([f"{i['cake_name']} (₱{float(i['price']):.0f})" for i in items])

    order_data = {
        "order_items":          items,
        "item":           item_names,
        "amount":         amount,
        "payment_method": payment_method,
        "cash_received":  cash_received,
        "change":         change,
        "order_source":   "walk-in",
        "cashier_id":     session.get('user_id'),
        "status":         "Completed",
        "created_at":     now
    }

    # Save to walkin_orders collection
    doc_ref = walkin_orders.add(order_data)
    order_id = doc_ref[1].id

    flash('Order placed successfully! 🎂', 'success')
    return redirect(url_for('pos.pos_receipt', order_id=order_id))


# ---------------- POS RECEIPT ----------------
@pos_bp.route('/pos/receipt/<order_id>')
def pos_receipt(order_id):
    current_user = session.get('user')
    if not current_user or not current_user.get('admin'):
        return redirect(url_for('auth_page'))

    order_doc = walkin_orders.document(order_id).get()
    if not order_doc.exists:
        flash('Receipt not found!', 'danger')
        return redirect(url_for('pos.pos_page'))

    order = order_doc.to_dict()
    order['id'] = order_id

    # Convert created_at
    created_at = order.get('created_at')
    if isinstance(created_at, datetime):
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc).astimezone(PH_TZ)
        else:
            created_at = created_at.astimezone(PH_TZ)
    order['created_at'] = created_at

    return render_template('pos_receipt.html', order=order)