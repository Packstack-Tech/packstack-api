import requests

from .consts import MAILGUN_API_KEY, MAILGUN_SENDING_DOMAIN, APP_HOST


def send_password_reset(email, token):
    url = f"https://api.mailgun.net/v3/{MAILGUN_SENDING_DOMAIN}/messages"
    auth = ("api", MAILGUN_API_KEY)
    data = {"from": f"Packstack <jerad@{MAILGUN_SENDING_DOMAIN}>",
            "to": [email],
            "subject": "Password reset request",
            "html": f'<p>We received a request to reset your password. If you made this request, <a href="{APP_HOST}/auth/reset-password/{token}" target="_blank">click here to reset your password.</a></p><p>If you did not request this, ignore this email.</p>'
            }
    data["h:Reply-To"] = "Jerad <jerad@packstack.io>"

    try:
        requests.post(url, auth=auth, data=data)
    except Exception as e:
        print(e.message)
        pass
