from pymongo import MongoClient

client = MongoClient("mongodb+srv://jualsolamilloui_db_user:j6KPGtuBJKCPeydM@cluster0.m5in6am.mongodb.net/?appName=Cluster00")

db = client["cake_shop_db"]
sales = db["sales"]
expenses = db["expenses"]
inventory = db["inventory"]
users = db["users"]