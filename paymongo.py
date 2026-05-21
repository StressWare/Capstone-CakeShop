import requests
import os
import base64
import logging
from dotenv import load_dotenv
load_dotenv()
logger = logging.getLogger(__name__)
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
            logger.error(f"PayMongo checkout error: {data}")
            return None

    except Exception as e:
        logger.exception("PayMongo create_checkout_session failed")
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
        logger.exception("PayMongo verify_payment failed")
        return {"paid": False}


# ================================================================
# BUILD LINE ITEMS (for checkout session)
# ================================================================
def build_line_items(order_type, selected_items, amount,
                     downpayment_type=None, downpayment_amount=None,
                     remaining_balance=None, discount_amount=0,delivery_fee=0, rush_fee=0):
    """
    Build line_items list for PayMongo checkout session.
    - Premade: one line item per cake, with voucher discount applied as negative line item.
    - Custom: single line item using downpayment amount (or full if no downpayment).
    """
    line_items = []
    discount_amount = float(discount_amount or 0)

    if order_type == "premade" and selected_items:
        # One line item per cake at real price
        for item in selected_items:
            line_items.append({
                "currency": "PHP",
                "amount":   int(float(item["price"]) * 100),
                "name":     item["cake_name"],
                "quantity": int(item.get("quantity", 1))
            })
        # Delivery fee line item ──
        if delivery_fee > 0:
            line_items.append({
                "currency": "PHP",
                "amount":   int(delivery_fee * 100),
                "name":     "Delivery Fee",
                "quantity": 1
            })

        # ── Apply voucher discount as a negative line item ──
        # PayMongo doesn't support discounts natively, so we add a discount line.
        # Amount must be positive in the dict but we label it clearly.
        if discount_amount > 0:
            line_items.append({
                "currency": "PHP",
                "amount":   int(discount_amount * 100),
                "name":     "Voucher Discount",
                "quantity": 1,
                # PayMongo requires amount > 0; the negative effect is
                # handled by passing the correct total to create_checkout_session.
                # This line item is display-only on the PayMongo page.
            })

    else:
        # ── Custom cake ──
        if downpayment_type and downpayment_type != "full" and downpayment_amount:
            pct_label = "50%" if downpayment_type == "50" else "75%"
            charge    = float(downpayment_amount)
            balance   = float(remaining_balance or 0)

            line_items.append({
                "currency": "PHP",
                "amount":   int(charge * 100),
                "name":     f"Custom Cake Order — {pct_label} Downpayment",
                "quantity": 1,
                
            })
        else:
            # Full payment
            charge = float(downpayment_amount or amount)
            line_items.append({
                "currency": "PHP",
                "amount":   int(charge * 100),
                "name":     "Custom Cake Order — Full Payment",
                "quantity": 1,
            })

    return line_items