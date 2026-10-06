"""Send an approved application only to an explicitly entered employer email."""

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Any

from app.applications.delivery import is_email_address

_MAX_RESUME_BYTES = 5_000_000


@dataclass(frozen=True, repr=False)
class SMTPSettings:
    host: str
    port: int
    security: str
    username: str
    password: str
    from_email: str
    resume_filename: str
    resume_bytes: bytes

    @classmethod
    def from_environment(cls, *, default_from_email: str | None = None) -> "SMTPSettings":
        names = ("SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "SMTP_USERNAME", "SMTP_PASSWORD",
                 "RESUME_PDF_PATH")
        missing = [name for name in names if not os.environ.get(name)]
        if not (os.environ.get("SMTP_FROM") or default_from_email):
            missing.append("SMTP_FROM or profile contact_email")
        if missing:
            raise ValueError("missing email configuration: " + ", ".join(missing))
        host = os.environ["SMTP_HOST"].strip()
        if not host or any(char in host for char in " /\\\r\n\t"):
            raise ValueError("invalid SMTP_HOST")
        try:
            port = int(os.environ["SMTP_PORT"])
        except ValueError as exc:
            raise ValueError("SMTP_PORT must be a valid port") from exc
        if not 1 <= port <= 65535:
            raise ValueError("SMTP_PORT must be a valid port")
        security = os.environ["SMTP_SECURITY"].strip().lower()
        if security not in {"ssl", "starttls"}:
            raise ValueError("SMTP_SECURITY must be ssl or starttls")
        from_email = (os.environ.get("SMTP_FROM") or default_from_email or "").strip()
        if not is_email_address(from_email):
            raise ValueError("SMTP_FROM must be a valid email address")
        resume_path = Path(os.environ["RESUME_PDF_PATH"])
        if not resume_path.is_absolute() or not resume_path.is_file():
            raise ValueError("RESUME_PDF_PATH must point to an existing absolute PDF file")
        size = resume_path.stat().st_size
        if not 1 <= size <= _MAX_RESUME_BYTES:
            raise ValueError("resume PDF must be at most 5 MB")
        resume_bytes = resume_path.read_bytes()
        if not resume_bytes.startswith(b"%PDF-"):
            raise ValueError("resume file is not a PDF")
        return cls(host, port, security, os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"],
                   from_email, resume_path.name, resume_bytes)


class SMTPApplicationSender:
    channel = "email"

    def __init__(self, settings: SMTPSettings | None = None) -> None:
        self.settings = settings

    def can_send(self, review: dict[str, Any]) -> bool:
        address = review.get("recipient_email")
        return isinstance(address, str) and is_email_address(address)

    def send(self, review: dict[str, Any]) -> str:
        if self.settings is None:
            raise ValueError("SMTP sender is in preview mode")
        if not self.can_send(review):
            raise ValueError("an explicit employer email is required")
        settings = self.settings
        title = " ".join(str(review["title"]).split())[:150]
        company = " ".join(str(review["company"]).split())[:150]
        message = EmailMessage()
        message["From"] = settings.from_email
        message["To"] = review["recipient_email"]
        message["Subject"] = f"Application: {title} - {company}"
        message_id = make_msgid()
        message["Message-ID"] = message_id
        message.set_content(review["draft_text"])
        message.add_attachment(settings.resume_bytes, maintype="application", subtype="pdf",
                               filename=settings.resume_filename)
        _send_message(settings, message)
        return message_id


def _send_message(settings: SMTPSettings, message: EmailMessage) -> None:
    context = ssl.create_default_context()
    if settings.security == "ssl":
        with smtplib.SMTP_SSL(settings.host, settings.port, timeout=20, context=context) as smtp:
            smtp.login(settings.username, settings.password)
            refused = smtp.send_message(message)
    else:
        with smtplib.SMTP(settings.host, settings.port, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(settings.username, settings.password)
            refused = smtp.send_message(message)
    if refused:
        raise RuntimeError("the SMTP server rejected one or more recipients")


def send_email(settings: SMTPSettings, recipient: str, subject: str, body: str) -> str:
    if not is_email_address(recipient):
        raise ValueError("invalid digest recipient")
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = settings.from_email, recipient, subject
    message["Message-ID"] = make_msgid()
    message.set_content(body)
    _send_message(settings, message)
    return str(message["Message-ID"])
