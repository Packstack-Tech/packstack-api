import mailchimp_marketing as MailchimpMarketing
import mailchimp_transactional as MailchimpTransactional
from mailchimp_marketing.api_client import ApiClientError

from utils.consts import MAILCHIMP_API_KEY, MAILCHIMP_AUDIENCE_ID, MANDRILL_API_KEY, APP_HOST


def mailchimp_connect():
    try:
        client = MailchimpMarketing.Client()
        client.set_config({
            "api_key": MAILCHIMP_API_KEY,
            "server": "us1"
        })
        return client
    except ApiClientError as error:
        print(error)


def mandrill_connect():
    try:
        client = MailchimpTransactional.Client(MANDRILL_API_KEY)
        return client
    except ApiClientError as error:
        print(error)


def add_contact(email, audience_id=MAILCHIMP_AUDIENCE_ID):
    client = mailchimp_connect()
    member_info = {
        "email_address": email,
        "status": "subscribed",
        "tags": ["V3"]
    }

    try:
        client.lists.add_list_member(audience_id, member_info)
    except ApiClientError as error:
        print("An exception occurred: {}".format(error.text))


def send_reset_password_email(email, token):
    client = mandrill_connect()
    message = {
        "from_email": "jerad@packstack.io",
        "from_name": "Packstack",
        "subject": "Password reset request",
        "to": [{"email": email}],
        "important": True,
        "track_opens": False,
        "html": f'<p>We received a request to reset your password. If you made this request, <a href="{APP_HOST}/auth/reset-password/{token}" target="_blank">click here to reset your password.</a></p><p>If you did not request this, ignore this email.</p>'
    }
    try:
        client.messages.send({"message": message})
    except ApiClientError as error:
        print("An exception occurred: {}".format(error.text))
