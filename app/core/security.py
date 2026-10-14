"""Security helpers for Slack request signature verification and authorization."""

import hashlib
import hmac
import logging
import time
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

SLACK_MAX_TIMESTAMP_DRIFT = 300  # 5 minutes in seconds


def verify_slack_signature(
    raw_body: bytes,
    timestamp: Optional[str],
    signature: Optional[str],
    signing_secret: Optional[str] = None,
    current_time: Optional[float] = None,
) -> bool:
    """Verify Slack HMAC-SHA256 request signature using raw body bytes.

    Specification:
    1. Check presence of timestamp and signature.
    2. Check timestamp drift: reject if timestamp is older or newer than 5 minutes.
    3. Compute HMAC-SHA256 of 'v0:{timestamp}:{raw_body}'.
    4. Secure comparison using hmac.compare_digest.

    Args:
        raw_body: Exact raw request body bytes.
        timestamp: Value of X-Slack-Request-Timestamp header.
        signature: Value of X-Slack-Signature header (e.g. 'v0=abcdef...').
        signing_secret: Optional Slack signing secret (falls back to settings.slack_signing_secret).
        current_time: Optional float timestamp override for deterministic testing.

    Returns:
        True if the signature is valid and within timestamp limits, False otherwise.
    """
    secret = signing_secret or get_settings().slack_signing_secret
    if not secret:
        logger.warning("Slack signature verification failed: SLACK_SIGNING_SECRET is not configured.")
        return False

    if not timestamp or not signature:
        logger.warning("Slack signature verification failed: Missing timestamp or signature header.")
        return False

    try:
        ts_int = int(timestamp)
    except (ValueError, TypeError):
        logger.warning("Slack signature verification failed: Non-integer timestamp '%s'.", timestamp)
        return False

    now = current_time if current_time is not None else time.time()
    if abs(now - ts_int) > SLACK_MAX_TIMESTAMP_DRIFT:
        logger.warning(
            "Slack signature verification failed: Timestamp drift too large (%s vs %s, drift %ss).",
            ts_int,
            int(now),
            abs(now - ts_int),
        )
        return False

    # Compute expected signature: v0=HMAC_SHA256(secret, b"v0:" + timestamp.encode() + b":" + raw_body)
    sig_basestring = b"v0:" + str(ts_int).encode("utf-8") + b":" + raw_body
    computed_hash = hmac.new(
        key=secret.encode("utf-8"),
        msg=sig_basestring,
        digestmod=hashlib.sha256,
    ).hexdigest()
    expected_signature = f"v0={computed_hash}"

    is_valid = hmac.compare_digest(expected_signature, signature)
    if not is_valid:
        logger.warning("Slack signature mismatch.")
    return is_valid
