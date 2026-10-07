"""Email: read new mail over IMAP, reply over SMTP. Reference: Hermes
plugins/platforms/email (MIT).

From addresses are easy to forge, so an allowed address only counts when
the receiving server vouched for it: the Authentication-Results header it
added must show dmarc=pass. Gmail, Outlook and Fastmail all add it."""
import asyncio
import email
import imaplib
import re
import smtplib
import ssl
import uuid
from email.message import EmailMessage
from email.utils import parseaddr

from core.connectors import register
from core.connectors.base import Connector, ConnectorError, Field, Inbound

_QUOTE_START = re.compile(r"^(On .+wrote:|-----Original Message-----|From: .+)$", re.MULTILINE)
SEP = "\x1f"  # conversation = address, subject and message ID, kept together


def _verified(message) -> bool:
    results = " ".join(message.get_all("Authentication-Results") or []).lower()
    return "dmarc=pass" in results


def _body(message) -> tuple[str, list]:
    text, files = "", []
    for part in message.walk() if message.is_multipart() else [message]:
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename()
        if filename:
            files.append((filename, part.get_payload(decode=True) or b""))
        elif part.get_content_type() == "text/plain" and not text:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return text, files


def split_reply(text: str) -> tuple[str, str]:
    """(what they wrote, what they quoted)."""
    match = _QUOTE_START.search(text)
    head, quoted = (text[:match.start()], text[match.start():]) if match else (text, "")
    fresh = "\n".join(line for line in head.splitlines() if not line.startswith(">")).strip()
    quoted += "\n" + "\n".join(line[1:].strip() for line in head.splitlines() if line.startswith(">"))
    return fresh, quoted.strip()


@register
class Email(Connector):
    kind = "email"
    label = "Email"
    description = "An inbox Kairos reads and answers. Use a dedicated address, not your main one."
    docs_url = "https://support.google.com/mail/answer/185833"
    message_limit = 50000
    target_field = "default_to"
    sender_help = ("Email addresses allowed to write to Kairos, one per line. Only mail your server marked "
                   "DMARC-pass counts, so a forged sender is ignored.")
    fields = (
        Field("address", "Email address", placeholder="jarvis@example.com"),
        Field("password", "Password or app password", secret=True, help="Gmail: an app password."),
        Field("imap_host", "IMAP server", placeholder="imap.gmail.com"),
        Field("imap_port", "IMAP port", kind="number", default="993", required=False),
        Field("smtp_host", "SMTP server", placeholder="smtp.gmail.com"),
        Field("smtp_port", "SMTP port", kind="number", default="465", required=False,
              help="465 for SSL, 587 for STARTTLS."),
        Field("poll_seconds", "Check every (seconds)", kind="number", default="60", required=False),
        Field("default_to", "Send notifications to", required=False, placeholder="you@example.com"),
    )

    async def run(self) -> None:
        interval = max(15, int(self.setting("poll_seconds", "60") or 60))
        while True:
            for inbound in await asyncio.to_thread(self._fetch_new):
                await self.hub.receive(self, inbound)
            await asyncio.sleep(interval)

    def _imap(self):
        return imaplib.IMAP4_SSL(self.setting("imap_host"), int(self.setting("imap_port", "993") or 993),
                                 ssl_context=ssl.create_default_context())

    def _fetch_new(self) -> list[Inbound]:
        found = []
        with self._imap() as box:
            box.login(self.setting("address"), self.setting("password"))
            box.select("INBOX")
            status, data = box.search(None, "UNSEEN")
            if status != "OK":
                raise ConnectorError("Searching the inbox failed")
            for number in (data[0] or b"").split():
                status, parts = box.fetch(number, "(RFC822)")
                if status != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                message = email.message_from_bytes(parts[0][1])
                address = parseaddr(message.get("From", ""))[1].lower()
                text, files = _body(message)
                fresh, quoted = split_reply(text)
                subject = message.get("Subject", "")
                # Unverified mail gets an address the allow list can never
                # contain, so it is ignored (or, on an open inbox, still
                # never reaches an agent).
                sender = address if _verified(message) else f"unverified:{address}"
                found.append(Inbound(conversation=SEP.join([address, subject, message.get("Message-ID", "")]),
                                     sender=sender, text=fresh, attachments=files, replying_to=quoted,
                                     sender_name=parseaddr(message.get("From", ""))[0]))
        return found

    async def send(self, conversation: str, text: str) -> None:
        await asyncio.to_thread(self._smtp_send, conversation, text)

    def _smtp_send(self, conversation: str, text: str) -> None:
        to, subject, in_reply_to = (conversation.split(SEP) + ["", ""])[:3]
        message = EmailMessage()
        message["From"] = self.setting("address")
        message["To"] = to
        message["Subject"] = subject if subject.lower().startswith("re:") else (f"Re: {subject}" if subject else "From Kairos")
        message["Message-ID"] = f"<{uuid.uuid4().hex}@jarvis>"
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
            message["References"] = in_reply_to
        message.set_content(text)
        port = int(self.setting("smtp_port", "465") or 465)
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(self.setting("smtp_host"), port, context=context) as server:
                server.login(self.setting("address"), self.setting("password"))
                server.send_message(message)
        else:
            with smtplib.SMTP(self.setting("smtp_host"), port) as server:
                server.starttls(context=context)
                server.login(self.setting("address"), self.setting("password"))
                server.send_message(message)
