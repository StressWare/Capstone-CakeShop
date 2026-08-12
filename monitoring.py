from flask import Blueprint, render_template
from datetime import datetime
from decorators import professor_required
from helpers import _today_range, PH_TZ
from db import orders, walkin_orders, login_logs, users

monitoring_bp = Blueprint("monitoring", __name__, url_prefix="/monitoring")

@monitoring_bp.route("/dashboard")
@professor_required
def dashboard():
    start, end = _today_range()

    premade_today = list(
        orders.where("created_at", ">=", start).where("created_at", "<", end)
        .where("order_type", "==", "premade").stream()
    )
    custom_today = list(
        orders.where("created_at", ">=", start).where("created_at", "<", end)
        .where("order_type", "==", "custom").stream()
    )
    walkin_today = list(
        walkin_orders.where("created_at", ">=", start).where("created_at", "<", end).stream()
    )
    logins_today = list(
        login_logs.where("timestamp", ">=", start).where("timestamp", "<", end).stream()
    )
    signups_today = list(
        users.where("created_at", ">=", start).where("created_at", "<", end).stream()
    )

    return render_template("monitoring_dashboard.html",
        premade_count=len(premade_today),
        custom_count=len(custom_today),
        walkin_order_count=len(walkin_today),
        total_transactions=len(premade_today) + len(custom_today) + len(walkin_today),
        login_count=len(logins_today),
        signup_count=len(signups_today),
        now=datetime.now(PH_TZ),
    )