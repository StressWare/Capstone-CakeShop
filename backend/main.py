from flask import Flask, request, jsonify, send_file
from bson.objectid import ObjectId
from db import inventory

app = Flask(__name__)

# Serve HTML
@app.route("/")
def index():
    return send_file("../frontend/admin.html")

# --- Inventory APIs ---
@app.route("/api/inventory", methods=["GET"])
def get_inventory():
    items = []
    for i in inventory.find():
        items.append({
            "id": str(i["_id"]),
            "item": i["item"],
            "quantity": i["quantity"],
            "cost": i["cost"]
        })
    return jsonify(items)

@app.route("/api/inventory", methods=["POST"])
def add_inventory():
    data = request.json
    result = inventory.insert_one({
        "item": data["item"],
        "quantity": data["quantity"],
        "cost": data["cost"]
    })
    return jsonify({"id": str(result.inserted_id)})

if __name__ == "__main__":
    app.run(debug=True)
