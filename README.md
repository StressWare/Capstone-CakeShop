# Mrs. Brave's Cakeshop
A web application built for Mrs. Brave's Cakeshop in Iloilo City. It handles online ordering, walk-in transactions, delivery and pickup, and business management through an admin dashboard.

---

## Group
**StressWare** — BSIT Capstone Project

---

## Features

### Customer Side
- Browse and order premade or customized cakes online
- Pay via PayMongo (GCash, Maya, Card, QR Ph)
- Choose delivery (within Iloilo City) or pickup at the shop, with an interactive map
- Register and log in via Firebase Authentication
- Leave reviews on completed orders
- Live 3D cake preview when customizing
- Chat support with predefined FAQ buttons; can escalate to live chat with the owner
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
- Receipt generation per transaction
- Daily/weekly sales history

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
| 3D Preview | Three.js |
| Rate Limiting | flask-limiter |
| Tunneling (dev) | ngrok / pyngrok |

---

## Project Structureshboard
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


## Map Coverage

Delivery is limited to Iloilo City only. The map uses Leaflet.js with OpenStreetMap and is bounded to the Iloilo area. If pickup is selected, the shop location is shown on the map instead.

---

## Notes

- Firebase frontend config is included in templates. Client-side Firebase keys are safe to expose; security is handled through Firestore Rules and Firebase Authentication.
- ngrok is used during development to expose a live URL for testing.
