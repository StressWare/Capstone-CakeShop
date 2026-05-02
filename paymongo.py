import requests
import os
import base64
from dotenv import load_dotenv
load_dotenv()

# ================================================================
# PAYMONGO CONFIG
# ================================================================
PAYMONGO_SECRET_KEY = os.getenv('PAYMONGO_SECRET_KEY')
PAYMONGO_BASE_URL   = 'https://api.paymongo.com/v1'

def get_auth_header():
    # Base64 encode secret key for PayMongo API auth
    encoded = base64.b64encode(f"{PAYMONGO_SECRET_KEY}:".encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "Content-Type":  "application/json",
        "Accept":        "application/json"
    }

# ================================================================
# CREATE CHECKOUT SESSION
# ================================================================
def create_checkout_session(amount, order_description, line_items, success_url, cancel_url):
    # Create a PayMongo checkout session. Returns: {checkout_url, session_id} or None if failed
    url = f"{PAYMONGO_BASE_URL}/checkout_sessions"
    payload = {
        "data": {
            "attributes": {
                "billing":              None,
                "cancel_url":           cancel_url,
                "description":          order_description,
                "line_items":           line_items,
                "merchant":             "Ms. Brave Cake Shop",
                "statement_descriptor": "MS BRAVE CAKES",
                "payment_method_types": ["gcash", "card", "paymaya", "qrph"],
                "send_email_receipt":   False,
                "show_description":     True,
                "show_line_items":      True,
                "success_url":          success_url,
            }
        }
    }

    try:
        response = requests.post(url, json=payload, headers=get_auth_header())
        data     = response.json()

        if response.status_code in {200, 201}:
            session_id   = data["data"]["id"]
            checkout_url = data["data"]["attributes"]["checkout_url"]
            return {
                "session_id":   session_id,
                "checkout_url": checkout_url
            }
        else:
            print(f"PayMongo error: {data}")
            return None

    except Exception as e:
        print(f"PayMongo create_checkout_session error: {str(e)}")
        return None


# ================================================================
# VERIFY PAYMENT (retrieve checkout session)
# ================================================================
def verify_payment(session_id):
    url = f"{PAYMONGO_BASE_URL}/checkout_sessions/{session_id}"
    try:
        response = requests.get(url, headers=get_auth_header())
        # Fail fast on HTTP errors
        response.raise_for_status()

        # Handle JSON decode errors explicitly
        try:
            data = response.json()
        except ValueError:
            return {"error": "Invalid response from payment provider"}

        attributes = data.get("data", {}).get("attributes", {})

        # ── Check payment intent status ──
        pi_status = attributes.get("payment_intent", {}) \
                              .get("attributes", {}) \
                              .get("status", "")

        # ── Check actual payment status ──
        payments       = attributes.get("payments", [])
        payment_status = ""
        payment_method = "Unknown"
        reference      = None
        
        if isinstance(payments, list) and payments:
            payment_status = payments[0].get("attributes", {}).get("status", "")
            payment_method = payments[0].get("attributes", {}).get("source", {}).get("type", "Unknown")
            reference      = payments[0].get("id", None)

        # ── Must be BOTH succeeded AND paid ──
        is_paid = pi_status == "succeeded" and payment_status == "paid"

        return {
            "paid":           is_paid,
            "payment_method": payment_method,
            "reference":      reference
        }

    except Exception as e:
        print(f"PayMongo verify error: {str(e)}")
        return {"paid": False}


# ================================================================
# BUILD LINE ITEMS (for checkout session)
# ================================================================
def build_line_items(order_type, selected_items, amount):
    # Build line_items list for PayMongo checkout session. Returns list of line item dicts.
    line_items = []

    if order_type == "premade" and selected_items:
        for item in selected_items:
            line_items.append({
                "currency": "PHP",
                "amount":   int(float(item["price"]) * 100),  # convert to centavos
                "name":     item["cake_name"],
                "quantity": int(item.get("quantity", 1))
            })
    else:
        # Custom cake → single line item
        line_items.append({
            "currency": "PHP",
            "amount":   int(float(amount) * 100),
            "name":     "Custom Cake Order",
            "quantity": 1
        })

    return line_items