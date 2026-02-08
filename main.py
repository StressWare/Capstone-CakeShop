from flask import Flask, render_template, request, redirect, url_for
from datetime import datetime, date
from db import inventory, expenses, sales,users  # sales collection added
from bson import ObjectId

app = Flask(__name__)
# Home Page
@app.route("/")
def home_page():
    return render_template("home.html")

#Login Authentication
@app.route("/authentication", methods=["GET", "POST"])
def auth():
    message = ""
    if request.method == "POST":
        action = request.form.get("action")
        username = request.form.get("username")
        password = request.form.get("password")
        number = request.form.get("number")
        address = request.form.get("address")

        if action == "login":
            user = users.find_one({"username": username, "password": password})
            if user:
                return redirect(url_for("home_page"))
            else:
                message = "Invalid username or password."
        elif action == "signup":
            if users.find_one({"username": username}):
                message = "Username already exists."
            else:
                users.insert_one({"username": username, "password": password, "number": number, "address": address})
                return redirect(url_for("home_page"))

    return render_template("authentication.html", message=message)

# ================= Admin Page =================
@app.route("/admin_dashboard")
def admin_page():
    inv_items = list(inventory.find())
    exp_items = list(expenses.find())
    sales_items = list(sales.find())

    # --- Calculate totals for summary cards ---
    total_sales = sum(item.get("amount", 0) for item in sales_items)
    total_expenses = sum(item.get("cost", 0) for item in exp_items)
    total_profit = total_sales - total_expenses

    return render_template(
        "admin.html",
        inventory=inv_items,
        expenses=exp_items,
        sales=sales_items,
        total_sales=total_sales,
        total_expenses=total_expenses,
        total_profit=total_profit
    )

@app.route("/admin_panel")
def panel_page():
    today = date.today().strftime("%Y-%m-%d")
    orders = list(sales.find().sort("delivery_date", 1))
    low_stock = list(inventory.find({"quantity": {"$lt": 10}}))

    return render_template(
        "panel.html",
        orders=orders,
        low_stock=low_stock,
        today=today
    )

@app.route("/order/status/<order_id>", methods=["POST"])
def update_order_status(order_id):
    new_status = request.form["status"]

    sales.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {"status": new_status}}
    )

    return redirect(url_for("panel_page"))

# ================= Add Inventory =================
@app.route("/inventory/add", methods=["POST"])
def add_inventory():
    item = request.form["item"]
    quantity = int(request.form["quantity"])
    cost = float(request.form["cost"])

    # Add to inventory
    inventory.insert_one({
        "item": item,
        "quantity": quantity,
        "cost": cost
    })

    # Add to expenses automatically
    expenses.insert_one({
        "date": datetime.now().strftime("%Y-%m-%d"),
        "description": item,
        "cost": cost
    })

    return redirect(url_for("admin_page"))


# ================= Edit Inventory =================
@app.route("/inventory/edit/<id>", methods=["POST"])
def edit_inventory(id):
    updated_item = request.form["item"]
    updated_quantity = int(request.form["quantity"])
    updated_cost = float(request.form["cost"])

    # Update inventory
    inventory.update_one(
        {"_id": ObjectId(id)}, 
        {"$set": {"item": updated_item, "quantity": updated_quantity, "cost": updated_cost}}
    )

    # Update latest expense for this item
    expenses.update_one(
        {"description": updated_item},
        {"$set": {"cost": updated_cost, "date": datetime.now().strftime("%Y-%m-%d")}}
    )

    return redirect(url_for("admin_page"))


@app.route("/order", methods=["POST"])
def place_order():
    sales.insert_one({
        "date": datetime.now().strftime("%Y-%m-%d"),
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
        }
    })
    return redirect(url_for("home_page"))

@app.route("/customer_dashboard")
def customerdashboard():
    customer = users.find_one()  # temporary: first user
    customer_orders = list(sales.find({"customer.name": customer["username"]}))

    return render_template(
        "customer_dashboard.html",
        customer=customer,
        orders=customer_orders
    )

if __name__ == "__main__":
    app.run(debug=True)
