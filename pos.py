from flask import Blueprint, render_template, request, redirect, url_for, session, flash, current_app
from datetime import datetime, timedelta, timezone, date
import json
from db import walkin_orders, cakes
from extensions import limiter
from decorators import admin_required
from helpers import log_admin_action


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

    items_json     = request.form.get('items', '[]')
    items          = json.loads(items_json)
    payment_method = request.form.get('payment_method', 'Cash')
    cash_received  = float(request.form.get('cash_received', 0))
    amount         = float(request.form.get('amount', 0))
    change         = cash_received - amount if payment_method == 'Cash' else 0

    if not items:
        flash('No items selected!', 'warning')
        return redirect(url_for('pos.pos_page'))

    item_names = ", ".join([
        f"{i['cake_name']} x{i.get('quantity', 1)} (₱{float(i['price']):.0f})"
        for i in items
    ])

    order_data = {
        "order_items":    items,
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

    doc_ref  = walkin_orders.add(order_data)
    order_id = doc_ref[1].id
    log_admin_action(
        action="Created POS order",
        target=f"Walk-in order {order_id} — {item_names}",
        category="pos"
    )
    for i in items:
        cake_ref = cakes.document(i["cake_id"])
        cake_doc = cake_ref.get()
        if cake_doc.exists:
            current_qty = cake_doc.to_dict().get("quantity", 0)
            ordered_qty = int(i.get("quantity", 1))
            new_qty     = max(0, current_qty - ordered_qty)
            cake_ref.update({
                "quantity": new_qty,
                "status":   new_qty > 0
            })

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
