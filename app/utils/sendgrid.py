# using SendGrid's Python Library
# https://github.com/sendgrid/sendgrid-python
import os
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY')
APP_HOST = os.environ.get('APP_HOST')


def send_reset_request(email, token):
    message = Mail(
        from_email='jerad@packstack.io',
        to_emails=email,
        subject='Reset your password',
        html_content=f'<p>We received a request to reset your password. If you made this request, <a href="{APP_HOST}/auth/reset-password/{token}" target="_blank">click here to reset your password.</a></p><p>If you did not request this, ignore this email.</p>')
    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        sg.send(message)
    except Exception as e:
        print(e.message)
