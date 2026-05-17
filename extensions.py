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

LOGO_URL = "https://res.cloudinary.com/dnystscn8/image/upload/v1779012991/logo_fburga.png"


def send_order_confirmation(fname, email, order_id, amount, payment_method):
    try:
        if payment_method == "Cash on Delivery":
            payment_note = f"Please prepare <b>₱{amount:,.2f}</b> upon delivery."
            
        else:
            payment_note = f"Payment of <b>₱{amount:,.2f}</b> received!"
            

        html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Order Confirmation – Mrs. Brave's Cakes</title>
</head>
<body style="margin:0; padding:0; background-color:#fdf0f5; font-family: 'Helvetica Neue', Arial, sans-serif;">

  <!-- Wrapper -->
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#fdf0f5; padding: 32px 0;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px; width:100%; background:#ffffff; border-radius:16px; overflow:hidden; box-shadow: 0 4px 24px rgba(214,51,132,0.10);">

          <!-- HEADER -->
          <tr>
            <td style="background: linear-gradient(135deg, #d63384 0%, #f06eaa 100%); padding: 36px 32px; text-align:center;">
              <img src="{LOGO_URL}"
                   alt="Mrs. Brave's Cakes"
                   width="72" height="72"
                   style="border-radius:50%; border: 3px solid rgba(255,255,255,0.5); object-fit:cover; margin-bottom:12px;"
                   onerror="this.style.display='none'">
              <h1 style="margin:0; color:#ffffff; font-size:26px; font-weight:700; letter-spacing:-0.5px;">
                Mrs. Brave's Cakes
              </h1>
              <p style="margin:6px 0 0; color:rgba(255,255,255,0.85); font-size:13px; letter-spacing:1px; text-transform:uppercase;">
                Iloilo City
              </p>
            </td>
          </tr>

          <!-- HERO MESSAGE -->
          <tr>
            <td style="padding: 32px 40px 0; text-align:center;">
              <h2 style="margin:0 0 8px; color:#d63384; font-size:22px; font-weight:700;">
                Thank you for your order, {fname}!
              </h2>
              <p style="margin:0; color:#888; font-size:14px; line-height:1.6;">
                We're so excited to bake something special just for you.<br>
                Your order has been received and is being processed.
              </p>
            </td>
          </tr>

          <!-- ORDER DETAILS CARD -->
          <tr>
            <td style="padding: 28px 40px;">
              <table width="100%" cellpadding="0" cellspacing="0"
                     style="background:#fdf0f5; border-radius:12px; overflow:hidden; border: 1px solid #f5c6db;">
                <tr>
                  <td style="background:#d63384; padding:12px 20px;">
                    <span style="color:#fff; font-size:13px; font-weight:700; letter-spacing:0.5px;">
                        ORDER DETAILS
                    </span>
                  </td>
                </tr>
                <tr>
                  <td style="padding:20px;">
                    <table width="100%" cellpadding="0" cellspacing="0">

                      <tr>
                        <td style="padding:8px 0; border-bottom:1px solid #f5c6db;">
                          <span style="color:#999; font-size:12px; text-transform:uppercase; letter-spacing:0.5px;">Order ID</span><br>
                          <span style="color:#333; font-size:15px; font-weight:700; font-family:monospace;">#{order_id}</span>
                        </td>
                      </tr>

                      <tr>
                        <td style="padding:8px 0; border-bottom:1px solid #f5c6db;">
                          <span style="color:#999; font-size:12px; text-transform:uppercase; letter-spacing:0.5px;">Amount</span><br>
                          <span style="color:#d63384; font-size:20px; font-weight:700;">₱{amount:,.2f}</span>
                        </td>
                      </tr>

                      <tr>
                        <td style="padding:8px 0; border-bottom:1px solid #f5c6db;">
                          <span style="color:#999; font-size:12px; text-transform:uppercase; letter-spacing:0.5px;">Payment Method</span><br>
                          <span style="color:#333; font-size:14px; font-weight:600;"> {payment_method}</span>
                        </td>
                      </tr>

                      <tr>
                        <td style="padding:10px 0 2px;">
                          <span style="color:#333; font-size:13px;">{payment_note}</span>
                        </td>
                      </tr>

                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- REVIEW CTA -->
          <tr>
            <td style="padding: 0 40px 28px; text-align:center;">
              <div style="background:#fff8fb; border: 1px dashed #f0a0c8; border-radius:12px; padding:20px;">
                <p style="margin:0 0 14px; color:#555; font-size:13px; line-height:1.6;">
                  Once you receive your cake, we'd love to hear from you! 🥰<br>
                  Don't forget to leave a review on your dashboard.
                </p>
                <a href="https://your-domain.com/customer_dashboard"
                   style="display:inline-block; background: linear-gradient(135deg, #d63384, #f06eaa);
                          color:#fff; text-decoration:none; font-size:13px; font-weight:700;
                          padding:10px 24px; border-radius:24px; letter-spacing:0.3px;">
                  ⭐ Leave a Review
                </a>
              </div>
            </td>
          </tr>

          <!-- DIVIDER -->
          <tr>
            <td style="padding: 0 40px;">
              <hr style="border:none; border-top:1px solid #f5e0ec; margin:0;">
            </td>
          </tr>

          <!-- FOOTER -->
          <tr>
            <td style="padding:24px 40px 32px; text-align:center;">
              <p style="margin:0 0 4px; color:#d63384; font-size:14px; font-weight:700;">
                Mrs. Brave's Cakes Iloilo
              </p>
              <p style="margin:0 0 4px; color:#aaa; font-size:12px;">
                📍 R Mapa Street, Mandurriao, Iloilo City
              </p>
              <p style="margin:0 0 4px; color:#aaa; font-size:12px;">
                📞 09563504486
              </p>
              <p style="margin:12px 0 0;">
                <a href="https://www.facebook.com/mrsbraveonline"
                   style="color:#d63384; font-size:12px; text-decoration:none;">
                  Facebook Page
                </a>
              </p>
              <p style="margin:16px 0 0; color:#ccc; font-size:11px;">
                Made with love 🩷 — You're receiving this because you placed an order with us.
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>

</body>
</html>
        """

        response = resend.Emails.send({
            "from": "onboarding@resend.dev",
            "to": email,
            "subject": f"Order Confirmed – Mrs. Brave's Cakes #{order_id}",
            "html": html
        })
        logger.info(f"EMAIL SENT — {response}")

    except Exception as e:
        logger.error(f"EMAIL ERROR — {e}")