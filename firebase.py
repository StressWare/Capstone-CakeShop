import firebase_admin
from firebase_admin import credentials
import os
from dotenv import load_dotenv
load_dotenv()

# Initialize Firebase Admin SDK
if not firebase_admin._apps:  # Ensure Firebase is initialized only once
    cred = credentials.Certificate(os.getenv('FIREBASE_KEY_PATH'))  # Update with correct path
    firebase_admin.initialize_app(cred)