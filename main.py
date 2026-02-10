from flask import Flask, render_template, request, redirect, url_for, session
from datetime import datetime, date, timedelta
from collections import defaultdict
import firebase #connection
from db import sales, expenses, inventory, users  # Firestore collections

app = Flask(__name__)
app.secret_key = "secretngani"

# ---------------- HOME PAGE ----------------
@app.route("/")
def home_page():
    return render_template("home.html")

# ---------------- LOGIN / SIGNUP ----------------
@app.route("/authentication", methods=["GET", "POST"])
def auth():
    message = ""

    if request.method == "POST":
        action = request.form.get("action")
        username = request.form.get("username")
        password = request.form.get("password")
        number = request.form.get("number")
        address = request.form.get("address")
        fname = request.form.get("fname") 

        if action == "login":
            user_query = users.where("username", "==", username)\
                              .where("password", "==", password)\
                              .limit(1).stream()
            
            user = None
            for doc in user_query:
                user = doc.to_dict()
                session["user_id"] = doc.id  # store document ID
                session["username"] = username

            if user:
                return redirect(url_for("customer_dashboard"))
            else:
                message = "Invalid username or password."

        elif action == "signup":
            exists_query = users.where("username", "==", username).limit(1).stream()
            exists = any(exists_query)
            if exists:
                message = "Username already exists."
            else:
                doc_ref = users.add({
                    "username": username,
                    "password": password,
                    "number": number,
                    "address": address,
                    "fname": fname
                })
                session["user_id"] = doc_ref.id
                session["username"] = username
                return redirect(url_for("customer_dashboard"))

    return render_template("authentication.html", message=message)

# ---------------- LOGOUT ----------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth"))

# ---------------- ADMIN DASHBOARD ----------------

from collections import defaultdict
from datetime import datetime, timedelta

@app.route("/admin_dashboard")
def admin_page():
    # Fetch data
    inv_items = [doc.to_dict() for doc in inventory.stream()]
    exp_items = [doc.to_dict() for doc in expenses.stream()]
    sales_items = []
    for user_doc in users.stream():
        orders_ref = users.document(user_doc.id).collection("orders").stream()
        for order_doc in orders_ref:
            order = order_doc.to_dict()
            order["customer_username"] = user_doc.to_dict().get("username", "")
            sales_items.append(order)

    # --- Weekly filter ---
    today = datetime.today()
    week_ago = today - timedelta(days=7)

    weekly_sales = defaultdict(float)
    weekly_expenses = defaultdict(float)
    weekly_profit = defaultdict(float)

    # Populate weekly sales
    for s in sales_items:
        if "date" in s:
            s_date = datetime.strptime(s["date"], "%Y-%m-%d")
            if s_date >= week_ago:
                day = s_date.strftime("%a")  # Mon, Tue...
                weekly_sales[day] += s.get("amount", 0)

    # Populate weekly expenses
    for e in exp_items:
        if "date" in e:
            e_date = datetime.strptime(e["date"], "%Y-%m-%d")
            if e_date >= week_ago:
                day = e_date.strftime("%a")
                weekly_expenses[day] += e.get("cost", 0)

    # Calculate weekly profit
    for day in weekly_sales:
        weekly_profit[day] = weekly_sales[day] - weekly_expenses.get(day, 0)

    return render_template(
        "admin.html",
        inventory=inv_items,
        expenses=exp_items,
        sales=sales_items,
        weekly_sales=dict(weekly_sales),
        weekly_expenses=dict(weekly_expenses),
        weekly_profit=dict(weekly_profit)
    )




# ---------------- ADMIN PANEL ----------------
@app.route("/admin_panel")
def panel_page():
    today = date.today().strftime("%d/%m/%Y")  # dd/mm/yyyy
    low_stock = [doc.to_dict() for doc in inventory.where("quantity", "<", 10).stream()]

    # Fetch all orders from all users
    orders = []
    for user_doc in users.stream():
        orders_ref = users.document(user_doc.id).collection("orders").order_by("delivery_date").stream()
        for order_doc in orders_ref:
            order = order_doc.to_dict()
            order["id"] = order_doc.id
            order["customer_username"] = user_doc.to_dict().get("username", "")
            orders.append(order)

    return render_template(
        "panel.html",
        orders=orders,
        low_stock=low_stock,
        today=today
    )
# ---------------- UPDATE ORDER STATUS ----------------
@app.route("/order/status/<user_id>/<order_id>", methods=["POST"])
def update_order_status(user_id, order_id):
    # Update order in the user's subcollection
    users.document(user_id).collection("orders").document(order_id).update({
        "status": request.form["status"]
    })
    return redirect(url_for("panel_page"))

# ---------------- ADD INVENTORY ----------------
@app.route("/inventory/add", methods=["POST"])
def add_inventory():
    item = request.form["item"]
    quantity = int(request.form["quantity"])
    cost = float(request.form["cost"])

    inventory.add({
        "item": item,
        "quantity": quantity,
        "cost": cost
    })

    expenses.add({
        "date": datetime.now().strftime("%Y-%m-%d"),
        "description": item,
        "cost": cost
    })

    return redirect(url_for("admin_page"))

# ---------------- EDIT INVENTORY ----------------
@app.route("/inventory/edit/<id>", methods=["POST"])
def edit_inventory(id):
    inventory.document(id).update({
        "item": request.form["item"],
        "quantity": int(request.form["quantity"]),
        "cost": float(request.form["cost"])
    })

    return redirect(url_for("admin_page"))

# ---------------- PLACE ORDER ----------------
@app.route("/order", methods=["POST"])
def place_order():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth"))

    now = datetime.now()
    formatted_time = now.strftime("%d/%m/%Y:%H/%M/%S")  # dd/mm/yyyy:hh/mm/ss

    order_data = {
        "delivery_date": request.form["delivery_date"],
        "item": request.form["order_item"],
        "amount": float(request.form["amount"]),
        "status": "New",
        "rush": True if request.form.get("rush") else False,
        "customer": {
            "name": request.form["customer_name"],
            "contact": request.form["contact"],
            "address": request.form["address"],
            "occasion": request.form["occasion"],
            "celebrant": request.form.get("celebrant"),
            "age": request.form.get("age")
        },
        "created_at": formatted_time,
        "created_at_ts": now
    }

    users.document(user_id).collection("orders").add(order_data)
    return redirect(url_for("customer_dashboard"))

# ---------------- CUSTOMER DASHBOARD ----------------
@app.route("/customer_dashboard")
def customer_dashboard():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth"))

    doc = users.document(user_id).get()
    if not doc.exists:
        return "User not found", 404
    customer = doc.to_dict()

    orders_ref = users.document(user_id).collection("orders").stream()
    orders = []
    for order_doc in orders_ref:
        order = order_doc.to_dict()
        order["id"] = order_doc.id
        orders.append(order)

    # Sort by timestamp descending
    orders.sort(key=lambda x: x.get("created_at_ts", datetime.min), reverse=True)

    return render_template(
        "customer_dashboard.html",
        customer=customer,
        orders=orders
    )

# ---------------- CUSTOMER PROFILE EDIT ROUTE ----------------
@app.route("/customer/edit", methods=["POST"])
def edit_customer_profile():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth"))

    updated_data = {
        "username": request.form.get("username"),
        "number": request.form.get("contact"),
        "address": request.form.get("address"),
        "full_name": request.form.get("full_name")
    }

    users.document(user_id).update(updated_data)
    return redirect(url_for("customer_dashboard"))


# ---------------- RUN SERVER ----------------
if __name__ == "__main__":
    app.run(debug=True)
