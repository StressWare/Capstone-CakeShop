
from firebase_admin import firestore

# Access Firestore Database from the firebase.py file
db = firestore.client()

# Define collections
sales = db.collection("sales")
expenses = db.collection("expenses")
inventory = db.collection("inventory")
users = db.collection("users")
cakes = db.collection("cakes")
walkin_orders = db.collection('walkin_orders')
reviews = db.collection('reviews')
admin_logs = db.collection("admin_logs")
orders = db.collection("orders")
notifications = db.collection("notifications")
pending_orders = db.collection("pending_orders")
custom_cake_price = db.collection("custom_cake_price")
fcm_tokens = db.collection("fcm_tokens")
conversations = db.collection("conversations")
locked_dates_ref = db.collection("locked_dates")