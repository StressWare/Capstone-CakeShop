import firebase_admin
from firebase_admin import credentials

# Initialize Firebase Admin SDK
if not firebase_admin._apps:  # Ensure Firebase is initialized only once
    cred = credentials.Certificate('firebase/serviceAccountKey.json')  # Update with correct path
    firebase_admin.initialize_app(cred)