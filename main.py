from flask import Flask, render_template, request, redirect, url_for
from datetime import datetime
from db import inventory, expenses  # Your MongoDB collections

app = Flask(__name__)

# ================= Admin Page =================
@app.route("/admin")
def admin_page():
    inv_items = list(inventory.find())
    exp_items = list(expenses.find())
    return render_template("admin.html", inventory=inv_items, expenses=exp_items)

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
        {"_id": id}, 
        {"$set": {"item": updated_item, "quantity": updated_quantity, "cost": updated_cost}}
    )

    # Update the latest expense for this item (optional: you can adjust logic if multiple entries)
    expenses.update_one(
        {"description": updated_item},
        {"$set": {"cost": updated_cost, "date": datetime.now().strftime("%Y-%m-%d")}}
    )

    return redirect(url_for("admin_page"))


if __name__ == "__main__":
    app.run(debug=True)
