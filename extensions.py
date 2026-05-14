# extensions.py
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import resend
import os
import logging
logger = logging.getLogger(__name__)
# Rate limiter
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# Resend
resend.api_key = os.environ.get("RESEND_API_KEY")

def send_order_confirmation(fname, email, order_id, amount, payment_method):
    try:
        if payment_method == "Cash on Delivery":
            payment_note = f"Please prepare ₱{amount:,.2f} upon delivery."
        else:
            payment_note = f"Payment of ₱{amount:,.2f} received! ✅"

        response = resend.Emails.send({
            "from": "onboarding@resend.dev",
            "to": email,
            "subject": "Thank you for your order! 🎂 — Mrs. Brave's Cakes",
            "html": f"""
                <h2>Hi {fname}! 🎂</h2>
                <p>Thank you for ordering with <b>Mrs. Brave's Cakes Iloilo!</b></p>
                <p>We're so excited to bake for you!</p>
                <hr>
                <p><b>Order ID:</b> #{order_id}</p>
                <p><b>Amount:</b> ₱{amount:,.2f}</p>
                <p><b>Payment:</b> {payment_method}</p>
                <p>{payment_note}</p>
                <hr>
                <p>Once you receive your cake, we'd love to hear from you!</p>
                <p>Don't forget to leave a review on your dashboard 🎂</p>
                <br>
                <p>With love,<br><b>Mrs. Brave's Cakes Iloilo</b></p>
            """
        })
        logger.info(f" EMAIL SENT — {response}")
    except Exception as e:
        logger.error(f" EMAIL ERROR — {e}")