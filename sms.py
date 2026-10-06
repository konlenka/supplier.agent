import logging

from twilio.rest import Client
from twilio.request_validator import RequestValidator

from config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER

logger = logging.getLogger(__name__)


def get_twilio_client() -> Client:
    return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def mask_phone(number: str) -> str:
    """A phone number as it may appear in a log line: its last three digits. Logs leave the
    machine for the host's log pipeline, and a staff member's mobile is personal information."""
    return f"***{number[-3:]}" if number else "(no number)"


def send_sms(to: str, body: str) -> str:
    """Send an SMS via Twilio. Returns the message SID."""
    client = get_twilio_client()
    message = client.messages.create(
        body=body,
        from_=TWILIO_PHONE_NUMBER,
        to=to,
    )
    logger.info("SMS sent to %s — SID: %s", mask_phone(to), message.sid)
    return message.sid


def validate_twilio_request(url: str, params: dict, signature: str) -> bool:
    """Validate that an incoming request is genuinely from Twilio."""
    if not TWILIO_AUTH_TOKEN:
        # An empty token signs like any other key, so with the secret missing on the host
        # anyone who knew the URL could post a stock count or an approver's yes. Refuse all.
        logger.error("TWILIO_AUTH_TOKEN is not set — refusing every incoming SMS")
        return False
    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    return validator.validate(url, params, signature)
