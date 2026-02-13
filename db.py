"""from pymongo import MongoClient


client = MongoClient("mongodb+srv://jualsolamilloui_db_user:j0Xie1TJ3YULCvUte@cluster0.m5in6am.mongodb.net/?appName=Cluster0")


db = client["cake_shop_db"]
sales = db["sales"]
expenses = db["expenses"]
inventory = db["inventory"]
users = db["users"]
"""

from firebase_admin import firestore

# Access Firestore Database from the firebase.py file
db = firestore.client()

# Define collections
sales = db.collection("sales")
expenses = db.collection("expenses")
inventory = db.collection("inventory")
users = db.collection("users")
cakes = db.collection("cakes")

