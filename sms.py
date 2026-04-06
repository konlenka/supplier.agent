import logging

from twilio.rest import Client
from twilio.request_validator import RequestValidator

from config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER

logger = logging.getLogger(__name__)


def get_twilio_client() -> Client:
    return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def send_sms(to: str, body: str) -> str:
    """Send an SMS via Twilio. Returns the message SID."""
    client = get_twilio_client()
    message = client.messages.create(
        body=body,
        from_=TWILIO_PHONE_NUMBER,
        to=to,
    )
    logger.info("SMS sent to %s — SID: %s", to, message.sid)
    return message.sid


def validate_twilio_request(url: str, params: dict, signature: str) -> bool:
    """Validate that an incoming request is genuinely from Twilio."""
    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    return validator.validate(url, params, signature)
