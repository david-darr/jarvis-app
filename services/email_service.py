"""Email: IMAP/SMTP account config + inbox/compose, the Email tab. Password
encrypted at rest via core/secret_storage, never returned in API responses
(masked instead, matching Odysseus's own account-config policy).

Google OAuth (XOAUTH2) for Gmail/Workspace accounts, scheduled send, and
AI summary/reply-draft/urgency extraction (all real Odysseus features) are
out of this pass — this is the plain IMAP/SMTP account + manual compose/send
foundation those would build on top of. Manual compose/send here is
direct-send (a real person clicking Send in a real email client), distinct
from agent-initiated sending, which should stay confirmation-first/draft-only
per the resolved harness principle — no agent email tool exists yet to need
that gate.
"""
import email
import imaplib
import smtplib
import time
import uuid
from datetime import date
from email.mime.text import MIMEText
from email.header import decode_header
from typing import Optional

from core.atomic_io import read_json, write_json_atomic
from core.constants import DATA_DIR
from core.secret_storage import encrypt, decrypt
import os

ACCOUNTS_FILE = os.path.join(DATA_DIR, "email_accounts.json")


class EmailService:
    def __init__(self) -> None:
        self._accounts: dict = read_json(ACCOUNTS_FILE, {})

    def _save(self) -> None:
        write_json_atomic(ACCOUNTS_FILE, self._accounts)

    def _masked(self, account: dict) -> dict:
        return {k: v for k, v in account.items() if k != "password_encrypted"}

    def list_accounts(self) -> list[dict]:
        return [self._masked(a) for a in self._accounts.values()]

    def get_account(self, account_id: str, decrypted: bool = False) -> Optional[dict]:
        account = self._accounts.get(account_id)
        if account is None:
            return None
        return account if decrypted else self._masked(account)

    def create_account(
        self, email_address: str, password: str,
        imap_host: str, imap_port: int,
        smtp_host: str, smtp_port: int,
        smtp_security: str = "ssl",
    ) -> dict:
        account_id = uuid.uuid4().hex[:12]
        account = {
            "id": account_id,
            "email": email_address,
            "password_encrypted": encrypt(password),
            "imap_host": imap_host,
            "imap_port": imap_port,
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
            "smtp_security": smtp_security,  # "ssl" | "starttls" | "none"
            "created_at": time.time(),
        }
        self._accounts[account_id] = account
        self._save()
        return self._masked(account)

    def delete_account(self, account_id: str) -> None:
        if account_id in self._accounts:
            del self._accounts[account_id]
            self._save()

    # -- IMAP/SMTP operations ------------------------------------------

    def test_connection(self, account_id: str) -> dict:
        account = self.get_account(account_id, decrypted=True)
        if account is None:
            raise KeyError(f"no such account: {account_id}")
        password = decrypt(account["password_encrypted"])

        result = {"imap": False, "smtp": False, "imap_error": None, "smtp_error": None}

        try:
            with imaplib.IMAP4_SSL(account["imap_host"], account["imap_port"], timeout=10) as imap:
                imap.login(account["email"], password)
            result["imap"] = True
        except Exception as e:
            result["imap_error"] = str(e)

        try:
            if account["smtp_security"] == "ssl":
                smtp = smtplib.SMTP_SSL(account["smtp_host"], account["smtp_port"], timeout=10)
            else:
                smtp = smtplib.SMTP(account["smtp_host"], account["smtp_port"], timeout=10)
                if account["smtp_security"] == "starttls":
                    smtp.starttls()
            smtp.login(account["email"], password)
            smtp.quit()
            result["smtp"] = True
        except Exception as e:
            result["smtp_error"] = str(e)

        return result

    def list_messages(
        self, account_id: str, folder: str = "INBOX", limit: int = 20,
        unseen_only: bool = False, since: Optional[date] = None,
    ) -> list[dict]:
        account = self.get_account(account_id, decrypted=True)
        if account is None:
            raise KeyError(f"no such account: {account_id}")
        password = decrypt(account["password_encrypted"])

        criteria = []
        if unseen_only:
            criteria.append("UNSEEN")
        if since is not None:
            criteria.append(f'SINCE "{since.strftime("%d-%b-%Y")}"')

        messages = []
        with imaplib.IMAP4_SSL(account["imap_host"], account["imap_port"], timeout=10) as imap:
            imap.login(account["email"], password)
            imap.select(folder, readonly=True)
            status, data = imap.search(None, *(criteria or ["ALL"]))
            if status != "OK":
                return []
            ids = data[0].split()[-limit:]
            for msg_id in reversed(ids):
                status, msg_data = imap.fetch(msg_id, "(FLAGS RFC822.HEADER)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                seen = b"\\Seen" in (msg_data[0][0] or b"")
                msg = email.message_from_bytes(msg_data[0][1])
                messages.append({
                    "uid": msg_id.decode(),
                    "from": _decode_header(msg.get("From", "")),
                    "subject": _decode_header(msg.get("Subject", "")),
                    "date": msg.get("Date", ""),
                    "seen": seen,
                })
        return messages

    def send_message(self, account_id: str, to: str, subject: str, body: str) -> None:
        account = self.get_account(account_id, decrypted=True)
        if account is None:
            raise KeyError(f"no such account: {account_id}")
        password = decrypt(account["password_encrypted"])

        msg = MIMEText(body)
        msg["From"] = account["email"]
        msg["To"] = to
        msg["Subject"] = subject

        if account["smtp_security"] == "ssl":
            smtp = smtplib.SMTP_SSL(account["smtp_host"], account["smtp_port"], timeout=10)
        else:
            smtp = smtplib.SMTP(account["smtp_host"], account["smtp_port"], timeout=10)
            if account["smtp_security"] == "starttls":
                smtp.starttls()
        try:
            smtp.login(account["email"], password)
            smtp.sendmail(account["email"], [to], msg.as_string())
        finally:
            smtp.quit()


def _decode_header(raw: str) -> str:
    if not raw:
        return ""
    parts = decode_header(raw)
    decoded = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            decoded += text.decode(enc or "utf-8", errors="replace")
        else:
            decoded += text
    return decoded


email_service = EmailService()
