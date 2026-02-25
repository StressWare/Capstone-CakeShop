import firebase_admin
from firebase_admin import credentials, auth, firestore
import os
from dotenv import load_dotenv
load_dotenv()

if not firebase_admin._apps:
    cred = credentials.Certificate(os.getenv('FIREBASE_KEY_PATH'))
    firebase_admin.initialize_app(cred)