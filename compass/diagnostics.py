"""Bounded provider diagnostics for the authenticated workspace; never credentials."""
import re


def redacted_detail(value, secrets=(), limit=500):
    message = str(value)
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        message = message.replace(secret, "[redacted]")
    message = re.sub(r"(?:https?|wss?)://[^\s<>]+", "[provider URL]", message)
    message = re.sub(r"(?i)\b(?:sk-|db-|td_live_)[A-Za-z0-9_.*-]+", "[redacted]", message)
    message = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", message)
    message = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[account]", message)
    return " ".join(message.split())[:limit]


def assistant_error(response, key):
    try:
        error = response.json().get("error", {})
    except (ValueError, AttributeError):
        error = {}
    if not isinstance(error, dict):
        error = {}
    code = error.get("code") or error.get("type") or "request_rejected"
    detail = error.get("message") or "Verify API credentials, permissions, model access and billing."
    return redacted_detail(f"OpenAI HTTP {response.status_code} ({code}): {detail}", (key,))
