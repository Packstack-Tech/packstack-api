import mailchimp_marketing as MailchimpMarketing
from mailchimp_marketing.api_client import ApiClientError

from consts import MAILCHIMP_API_KEY, MAILCHIMP_AUDIENCE_ID


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


def add_contact(email, audience_id=MAILCHIMP_AUDIENCE_ID):
    client = mailchimp_connect()
    member_info = {
        "email_address": email,
        "status": "subscribed",
        "tags": ["Social"]
    }

    try:
        client.lists.add_list_member(audience_id, member_info)
    except ApiClientError as error:
        print("An exception occurred: {}".format(error.text))
