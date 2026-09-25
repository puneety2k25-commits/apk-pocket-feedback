"""Private feedback relay for APK Pocket.

The Android app knows only the HTTPS endpoint. The publisher's receiving address and
SMTP credentials stay on this server and are never returned to the app.
"""
from __future__ import annotations
import json
import os
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from typing import Any, Callable

SCHEMA = "apk-pocket-feedback-v1"
CATEGORIES = {"Suggestion", "Bug report", "Other"}
EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")


class InvalidFeedback(ValueError):
    pass


class MailSender:
    def __init__(self, host: str, port: int, username: str, password: str,
                 security: str, sender: str, destination: str) -> None:
        if not host or not sender or not destination:
            raise ValueError("SMTP host, sender and destination are required")
        if security not in {"starttls", "ssl", "plain"}:
            raise ValueError("SMTP_SECURITY must be starttls, ssl or plain")
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.security, self.sender, self.destination = security, sender, destination

    def __call__(self, subject: str, body: str, reply_to: str) -> None:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = self.destination
        message["Subject"] = subject
        if reply_to:
            message["Reply-To"] = reply_to
        message.set_content(body)
        context = ssl.create_default_context()
        if self.security == "ssl":
            client = smtplib.SMTP_SSL(self.host, self.port, timeout=15, context=context)
        else:
            client = smtplib.SMTP(self.host, self.port, timeout=15)
        try:
            client.ehlo()
            if self.security == "starttls":
                client.starttls(context=context)
                client.ehlo()
            if self.username:
                client.login(self.username, self.password)
            client.send_message(message)
        finally:
            try:
                client.quit()
            except Exception:
                client.close()


class FeedbackService:
    def __init__(self, sender: Callable[[str, str, str], None]) -> None:
        self.sender = sender

    @staticmethod
    def validate(data: dict[str, Any]) -> dict[str, str]:
        required = {"schema", "category", "message", "contactEmail", "appVersion", "androidVersion", "deviceModel"}
        if not isinstance(data, dict) or set(data) != required or not all(isinstance(data[k], str) for k in required):
            raise InvalidFeedback("Invalid fields")
        if data["schema"] != SCHEMA or data["category"] not in CATEGORIES:
            raise InvalidFeedback("Invalid feedback type")
        message = data["message"].strip()
        email = data["contactEmail"].strip()
        if not message or len(message) > 4000:
            raise InvalidFeedback("Invalid message")
        if email and (len(email) > 254 or not EMAIL_RE.fullmatch(email)):
            raise InvalidFeedback("Invalid reply email")
        for key, maximum in (("appVersion", 64), ("androidVersion", 128), ("deviceModel", 160)):
            value = data[key].strip()
            if len(value) > maximum or "\r" in value or "\n" in value:
                raise InvalidFeedback("Invalid metadata")
        return {
            "category": data["category"], "message": message, "contactEmail": email,
            "appVersion": data["appVersion"].strip(), "androidVersion": data["androidVersion"].strip(),
            "deviceModel": data["deviceModel"].strip(),
        }

    def submit(self, raw: dict[str, Any]) -> None:
        data = self.validate(raw)
        lines = [
            "APK Pocket feedback", "", f"Category: {data['category']}",
            f"App version: {data['appVersion'] or 'not supplied'}",
        ]
        if data["contactEmail"]:
            lines.append("Reply requested: yes")
        else:
            lines.append("Reply requested: no")
        if data["androidVersion"] or data["deviceModel"]:
            lines += ["", "Device details supplied by user:", data["androidVersion"], data["deviceModel"]]
        lines += ["", "Message:", data["message"]]
        self.sender(f"APK Pocket feedback · {data['category']}", "\n".join(lines), data["contactEmail"])


class RateLimiter:
    """Small per-process safety net. Production should also rate-limit at the HTTPS edge."""
    def __init__(self, limit: int = 8, window: int = 600) -> None:
        self.limit, self.window, self.hits = limit, window, {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self.hits.get(key, []) if now - t < self.window]
        if len(recent) >= self.limit:
            self.hits[key] = recent
            return False
        recent.append(now)
        self.hits[key] = recent
        if len(self.hits) > 5000:
            cutoff = now - self.window
            self.hits = {k: [t for t in v if t >= cutoff] for k, v in self.hits.items() if any(t >= cutoff for t in v)}
        return True


def make_wsgi(service: FeedbackService, limiter: RateLimiter | None = None):
    limiter = limiter or RateLimiter()

    def application(environ, start_response):
        path, method = environ.get("PATH_INFO", ""), environ.get("REQUEST_METHOD", "")
        status, result = "200 OK", {"ok": True}
        try:
            if path == "/health" and method == "GET":
                result = {"status": "ready"}
            elif path == "/feedback" and method == "POST":
                if environ.get("CONTENT_TYPE", "").split(";")[0].strip() != "application/json":
                    raise InvalidFeedback("Expected JSON")
                length = int(environ.get("CONTENT_LENGTH", "0"))
                if not 0 < length <= 16_384:
                    raise InvalidFeedback("Invalid request size")
                client = environ.get("REMOTE_ADDR", "unknown")
                if not limiter.allow(client):
                    status, result = "429 Too Many Requests", {"error": "rate_limited"}
                else:
                    body = environ["wsgi.input"].read(length)
                    if len(body) != length:
                        raise InvalidFeedback("Incomplete request")
                    service.submit(json.loads(body))
                    status, result = "204 No Content", None
            else:
                status, result = "404 Not Found", {"error": "not_found"}
        except (InvalidFeedback, ValueError, TypeError, KeyError, json.JSONDecodeError):
            status, result = "400 Bad Request", {"error": "invalid_request"}
        except Exception:
            # Never return/log SMTP credentials, destination address, user message or reply email.
            status, result = "503 Service Unavailable", {"error": "delivery_unavailable"}

        data = b"" if result is None else json.dumps(result, separators=(",", ":")).encode("utf-8")
        headers = [("Content-Type", "application/json"), ("Content-Length", str(len(data))),
                   ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff")]
        start_response(status, headers)
        return [data]
    return application


def from_environment() -> FeedbackService:
    host = os.environ["SMTP_HOST"].strip()
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    security = os.environ.get("SMTP_SECURITY", "starttls").strip().lower()
    sender = os.environ["FEEDBACK_FROM_EMAIL"].strip()
    destination = os.environ["FEEDBACK_TO_EMAIL"].strip()
    if not EMAIL_RE.fullmatch(sender) or not EMAIL_RE.fullmatch(destination):
        raise ValueError("Server email configuration is invalid")
    return FeedbackService(MailSender(host, port, username, password, security, sender, destination))
