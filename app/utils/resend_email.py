import resend

from .consts import RESEND_API_KEY, APP_HOST

resend.api_key = RESEND_API_KEY

FROM_EMAIL = "Packstack <jerad@packstack.io>"


def send_verification_email(email, token):
    try:
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": [email],
            "subject": "Verify your email address",
            "html": (
                "<p>Thanks for signing up for Packstack!</p>"
                f'<p><a href="{APP_HOST}/auth/verify-email/{token}" target="_blank">'
                "Click here to verify your email address.</a></p>"
                "<p>If you did not create an account, you can ignore this email.</p>"
            ),
        })
    except Exception as e:
        print(f"Failed to send verification email: {e}")


def send_password_reset(email, token):
    try:
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": [email],
            "subject": "Password reset request",
            "html": (
                "<p>We received a request to reset your password. "
                f'If you made this request, <a href="{APP_HOST}/auth/reset-password/{token}" target="_blank">'
                "click here to reset your password.</a></p>"
                "<p>If you did not request this, ignore this email.</p>"
            ),
        })
    except Exception as e:
        print(f"Failed to send password reset email: {e}")
