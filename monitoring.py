from flask import Blueprint, render_template, request, flash
from datetime import datetime, timedelta
from decorators import professor_required
from helpers import PH_TZ
from db import orders, walkin_orders, login_logs, users

monitoring_bp = Blueprint("monitoring", __name__, url_prefix="/monitoring")

@monitoring_bp.route("/dashboard")
@professor_required
def dashboard():
    today = datetime.now(PH_TZ).replace(hour=0, minute=0, second=0, microsecond=0)

    date_str = request.args.get("date")
    try:
        if date_str:
            start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=PH_TZ)
        else:
            start = today
    except ValueError:
        flash("Invalid date, showing today instead.", "warning")
        start = today

    end = start + timedelta(days=1)

    premade = list(
        orders.where("created_at", ">=", start).where("created_at", "<", end)
        .where("order_type", "==", "premade").stream()
    )
    custom = list(
        orders.where("created_at", ">=", start).where("created_at", "<", end)
        .where("order_type", "==", "custom").stream()
    )
    walkin = list(
        walkin_orders.where("created_at", ">=", start).where("created_at", "<", end).stream()
    )
    logins = list(
        login_logs.where("timestamp", ">=", start).where("timestamp", "<", end).stream()
    )
    signups = list(
        users.where("created_at", ">=", start).where("created_at", "<", end).stream()
    )

    # Manual (Messenger) orders are stored as custom orders, so split them out
    manual_count = sum(1 for d in custom if d.to_dict().get("order_source") == "messenger")
    online_custom_count = len(custom) - manual_count

    # Unique customers only: skips staff logins and repeat logins
    customer_ids = {
        l.to_dict().get("user_id") for l in logins
        if not l.to_dict().get("is_staff")
    }

    return render_template("monitoring_dashboard.html",
        premade_count=len(premade),
        custom_count=online_custom_count,
        manual_count=manual_count,
        online_order_count=len(premade) + online_custom_count,
        walkin_order_count=len(walkin),
        total_transactions=len(premade) + len(custom) + len(walkin),
        login_count=len(customer_ids),
        signup_count=len(signups),
        selected_date=start.strftime("%Y-%m-%d"),
        now=datetime.now(PH_TZ),
        today_str=today.strftime("%Y-%m-%d"),
    )