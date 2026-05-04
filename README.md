# Mrs. Braves Cakeshop

A web application built for Mrs. Braves Cakeshop in Iloilo City. It handles online ordering, walk-in transactions, delivery and pickup, and business management through an admin dashboard.

---

## Group

**StressWare** — BSIT Capstone Project

---

## Features

### Customer Side
- Browse and order cakes online
- Pay via PayMongo (sandbox mode)
- Choose delivery (within Iloilo City) or pickup at the shop, with an interactive map
- Register and log in via Firebase Authentication
- Leave reviews on orders
- Chat support with predefined FAQ buttons; can escalate to live chat with the owner if needed
- Order status notifications

### Admin Dashboard
- Dashboard and analytics overview
- Sales and expenses tracking
- Inventory management
- Cake availability management
- Order list and management
- User management
- Reviews management
- Activity logs
- Conversations — respond to escalated customer chats

### POS System
- Separate POS for processing walk-in orders in-store

---

## Tech Stack

| Category | Technology |
|---|---|
| Language | Python |
| Framework | Flask |
| Frontend | HTML, CSS, JavaScript |

## Libraries & APIs

| Category | Technology |
|---|---|
| Database | Firebase Firestore |
| Authentication | Firebase Authentication |
| Image Storage | Cloudinary |
| Payments | PayMongo |
| Map | Leaflet.js + OpenStreetMap |
| 3D / Visual | Three.js |
| Tunneling (dev) | ngrok / pyngrok |

---

## Project Structure

```
Capstone-CakeShop/
├── static/             # CSS, JS, images
├── templates/          # HTML templates (Jinja2)
├── firebase/           # Firebase config (service account — gitignored)
├── main.py             # Main Flask app and routes
├── db.py               # Firestore collections
├── firebase.py         # Firebase Admin SDK initialization
├── paymongo.py         # PayMongo payment logic
├── pos.py              # POS system routes
├── requirements.txt    # Python dependencies
├── .env                # Environment variables (gitignored)
├── .gitignore          # Files to ignore when pushing to GitHub
└── .env.example        # Environment variable template
```

---



## Environment Variables

See `.env.example` for all required variables:

```
FIREBASE_KEY_PATH=path/to/serviceAccountKey.json
SECRET_KEY=your_flask_secret_key_here
CLOUDINARY_CLOUD_NAME=your_cloud_name_here
CLOUDINARY_API_KEY=your_cloudinary_api_key_here
CLOUDINARY_API_SECRET=your_cloudinary_api_secret_here
PAYMONGO_PUBLIC_KEY=your_paymongo_public_key_here
PAYMONGO_SECRET_KEY=your_paymongo_secret_key_here
```

---

## Map Coverage

Delivery is limited to Iloilo City only. The map uses Leaflet.js with OpenStreetMap and is bounded to the Iloilo area. If pickup is selected, the shop location is shown on the map instead.

---

## Notes

- Firebase frontend config is included in templates. Client-side Firebase keys are safe to expose; security is handled through Firestore Rules and Firebase Authentication.
- ngrok is used during development to expose a live URL for testing.
