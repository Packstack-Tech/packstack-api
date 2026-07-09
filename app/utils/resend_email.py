import logging

import resend
import sentry_sdk

from .consts import RESEND_API_KEY, APP_HOST

logger = logging.getLogger(__name__)

resend.api_key = RESEND_API_KEY

FROM_EMAIL = "Packstack <jerad@packstack.io>"

EMAIL_WRAPPER = """
<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 520px; margin: 0 auto; padding: 40px 24px;">
  <div style="margin-bottom: 32px; padding-bottom: 20px; border-bottom: 2px solid #e5e7eb;">
    <span style="font-size: 22px; font-weight: 700; color: #111827; letter-spacing: -0.02em;">Packstack</span>
  </div>
  <div style="color: #374151; font-size: 15px; line-height: 1.7;">
    {body}
  </div>
  <div style="margin-top: 40px; padding-top: 20px; border-top: 1px solid #e5e7eb; font-size: 12px; color: #9ca3af;">
    &copy; Packstack. All rights reserved.
  </div>
</div>
""".strip()

BUTTON_STYLE = (
    "display: inline-block; padding: 10px 24px; background-color: #111827; "
    "color: #ffffff; text-decoration: none; border-radius: 6px; font-weight: 600; font-size: 14px;"
)


def _wrap(body: str) -> str:
    return EMAIL_WRAPPER.format(body=body)


def create_contact(email: str, first_name: str = "", last_name: str = ""):
    try:
        params: resend.Contacts.CreateParams = {
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
        }
        resend.Contacts.create(params)
    except Exception as e:
        logger.exception("Failed to create Resend contact")
        sentry_sdk.capture_exception(e)


def delete_contact(email: str):
    try:
        resend.Contacts.remove({"email": email})
    except Exception:
        logger.warning("Failed to delete Resend contact for %s", email)


def send_verification_email(email, token):
    body = (
        "<p>Thanks for signing up for Packstack!</p>"
        "<p>Please confirm your email address to get started.</p>"
        f'<p style="margin: 28px 0;"><a href="{APP_HOST}/auth/verify-email/{token}" '
        f'target="_blank" style="{BUTTON_STYLE}">Verify email address</a></p>'
        '<p style="color: #6b7280; font-size: 13px;">'
        "If you did not create an account, you can safely ignore this email.</p>"
    )
    try:
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": [email],
            "subject": "Verify your email address",
            "html": _wrap(body),
        })
    except Exception as e:
        logger.exception("Failed to send verification email")
        sentry_sdk.capture_exception(e)


def send_welcome_email(email):
    link_style = "color: #111827; font-weight: 600; text-decoration: underline;"
    body = (
        '<p style="font-size: 18px; font-weight: 700; color: #111827; margin-top: 0;">'
        "Welcome to Packstack, and thanks for signing up!</p>"

        "<p>More than 3,000 backpackers and outdoor enthusiasts are using Packstack "
        "to create accurate packing lists, estimate calorie requirements and manage "
        "their gear. I&rsquo;m happy to count you among them.</p>"

        "<p><strong>To get started,</strong> you&rsquo;ll have to enter the gear you own. "
        "This is the laborious part, but it&rsquo;s worth it. To make it a little easier, "
        "we&rsquo;ll automatically populate the item category and weight when you enter "
        "the manufacturer and product.</p>"

        "<p>If you have an existing gear spreadsheet, you can upload it through the "
        f'<a href="https://app.packstack.io/" target="_blank" style="{link_style}">web app</a>.</p>'

        f'<p style="margin: 28px 0;"><a href="https://app.packstack.io/" '
        f'target="_blank" style="{BUTTON_STYLE}">Add your gear</a></p>'

        "<p>Packstack is available on "
        '<a href="https://apps.apple.com/us/app/packstack-app/id6760873801" '
        f'target="_blank" style="{link_style}">iOS</a>, '
        '<a href="https://play.google.com/store/apps/details?id=io.packstack.mobile" '
        f'target="_blank" style="{link_style}">Android</a> and the '
        f'<a href="https://app.packstack.io/" target="_blank" style="{link_style}">web</a>. '
        "Your data will automatically stay in sync across devices.</p>"

        "<p>If you ever have any questions, feedback or feature requests, just respond "
        "to this email. And don&rsquo;t forget to check out our free "
        '<a href="https://www.packstack.io/tools/ultralight-research" '
        f'target="_blank" style="{link_style}">Ultralight Gear Research</a> tool '
        "when you&rsquo;re on the hunt for new gear.</p>"

        '<p style="margin-top: 32px; margin-bottom: 0;">Jerad Maplethorpe</p>'
        '<p style="margin-top: 2px; color: #6b7280; font-size: 13px;">Founder, Packstack</p>'
    )
    try:
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": [email],
            "subject": "Welcome to Packstack",
            "html": _wrap(body),
        })
    except Exception as e:
        logger.exception("Failed to send welcome email")
        sentry_sdk.capture_exception(e)


def send_otp_email(email, otp_code):
    code_style = (
        "display: inline-block; font-size: 32px; font-weight: 700; "
        "letter-spacing: 0.3em; color: #111827; padding: 16px 24px; "
        "background-color: #f3f4f6; border-radius: 8px;"
    )
    body = (
        "<p>Use the code below to sign in to Packstack.</p>"
        f'<p style="margin: 28px 0; text-align: center;">'
        f'<span style="{code_style}">{otp_code}</span></p>'
        '<p style="color: #6b7280; font-size: 13px;">'
        "This code expires in 10 minutes. If you did not request this, "
        "you can safely ignore this email.</p>"
    )
    try:
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": [email],
            "subject": f"Your Packstack login code is {otp_code}",
            "html": _wrap(body),
        })
    except Exception as e:
        logger.exception("Failed to send OTP email")
        sentry_sdk.capture_exception(e)
