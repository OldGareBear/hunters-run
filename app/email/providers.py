"""Pluggable email providers. All of them take one fully rendered message."""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from email.utils import make_msgid
from functools import lru_cache
from typing import Protocol

import httpx

from app.config import Settings, get_settings

log = logging.getLogger(__name__)


@dataclass
class OutgoingEmail:
    to: str
    subject: str
    text: str
    html: str = ""
    kind: str = "transactional"  # transactional | broadcast
    headers: dict[str, str] = field(default_factory=dict)


class EmailSendError(Exception):
    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class EmailProvider(Protocol):
    def send(self, msg: OutgoingEmail) -> str:
        """Send one message; return the provider's message id."""
        ...


class ConsoleProvider:
    """Development provider: logs messages and keeps them in memory (used by tests)."""

    def __init__(self) -> None:
        self.sent: list[OutgoingEmail] = []

    def send(self, msg: OutgoingEmail) -> str:
        self.sent.append(msg)
        log.info("EMAIL to=%s subject=%r\n%s", msg.to, msg.subject, msg.text)
        return f"console-{len(self.sent)}"


def _mime(s: Settings, msg: OutgoingEmail) -> MimeMessage:
    m = MimeMessage()
    m["From"] = s.email_from
    m["To"] = msg.to
    m["Subject"] = msg.subject
    m["Message-ID"] = make_msgid()
    if s.email_reply_to:
        m["Reply-To"] = s.email_reply_to
    for k, v in msg.headers.items():
        m[k] = v
    m.set_content(msg.text)
    if msg.html:
        m.add_alternative(msg.html, subtype="html")
    return m


class SMTPProvider:
    def __init__(self, s: Settings) -> None:
        self.s = s

    def send(self, msg: OutgoingEmail) -> str:
        s = self.s
        mime = _mime(s, msg)
        try:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
                if s.smtp_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if s.smtp_username:
                    smtp.login(s.smtp_username, s.smtp_password)
                smtp.send_message(mime)
        except smtplib.SMTPRecipientsRefused as exc:
            raise EmailSendError(f"recipient refused: {exc}", permanent=True) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise EmailSendError(f"SMTP error: {exc}") from exc
        return str(mime["Message-ID"])


class SESProvider:
    def __init__(self, s: Settings) -> None:
        import boto3

        self.s = s
        self.client = boto3.client("sesv2", region_name=s.ses_region)

    def send(self, msg: OutgoingEmail) -> str:
        from botocore.exceptions import ClientError

        mime = _mime(self.s, msg)
        kwargs: dict = {
            "FromEmailAddress": self.s.email_from,
            "Destination": {"ToAddresses": [msg.to]},
            "Content": {"Raw": {"Data": mime.as_bytes()}},
        }
        if self.s.ses_configuration_set:
            kwargs["ConfigurationSetName"] = self.s.ses_configuration_set
        try:
            res = self.client.send_email(**kwargs)
        except ClientError as exc:
            code = exc.response["Error"].get("Code", "")
            permanent = code in {"MessageRejected", "MailFromDomainNotVerifiedException"}
            raise EmailSendError(f"SES error: {code}", permanent=permanent) from exc
        return res.get("MessageId", "")


class PostmarkProvider:
    API = "https://api.postmarkapp.com/email"

    def __init__(self, s: Settings) -> None:
        self.s = s
        self.http = httpx.Client(timeout=30)

    def send(self, msg: OutgoingEmail) -> str:
        s = self.s
        stream = (
            s.postmark_broadcast_stream if msg.kind == "broadcast" else s.postmark_message_stream
        )
        payload = {
            "From": s.email_from,
            "To": msg.to,
            "Subject": msg.subject,
            "TextBody": msg.text,
            "MessageStream": stream,
            "Headers": [{"Name": k, "Value": v} for k, v in msg.headers.items()],
        }
        if msg.html:
            payload["HtmlBody"] = msg.html
        if s.email_reply_to:
            payload["ReplyTo"] = s.email_reply_to
        try:
            res = self.http.post(
                self.API,
                json=payload,
                headers={
                    "Accept": "application/json",
                    "X-Postmark-Server-Token": s.postmark_server_token,
                },
            )
        except httpx.HTTPError as exc:
            raise EmailSendError(f"Postmark connection error: {exc}") from exc
        if res.status_code == 200:
            return res.json().get("MessageID", "")
        permanent = res.status_code == 422  # invalid/inactive recipient etc.
        raise EmailSendError(f"Postmark {res.status_code}: {res.text[:200]}", permanent=permanent)


@lru_cache
def get_provider() -> EmailProvider:
    s = get_settings()
    if s.email_provider == "smtp":
        return SMTPProvider(s)
    if s.email_provider == "ses":
        return SESProvider(s)
    if s.email_provider == "postmark":
        return PostmarkProvider(s)
    return ConsoleProvider()
