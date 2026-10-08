"""Read-only CRM collection. Transport identifiers are assigned here, not by AI."""
import email
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr
from html.parser import HTMLParser

from core import tab_api
from core.tab_api import message_time

api = tab_api.for_tab(__package__)

MAX_BYTES = 1_000_000
MAX_TEXT = 18000


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("br", "p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def header(value):
    try:
        return str(make_header(decode_header(value or "")))
    except (LookupError, UnicodeError):
        return value or ""


def text_body(msg):
    plain, html = [], []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment" or part.get_filename():
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        try:
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        if content_type == "text/plain":
            plain.append(text)
        else:
            parser = PlainHTML()
            parser.feed(text)
            html.append("".join(parser.parts))
    return "\n".join(plain or html).strip()


def _literal(parts):
    return next((part[1] for part in parts or [] if isinstance(part, tuple) and isinstance(part[1], bytes)), None)


def read_mailbox(source, settings, known):
    account = next((a for a in tab_api.email_accounts() if a["id"] == source["connection_id"]), None)
    if account is None:
        raise ValueError("The connected email account was removed")
    since = (datetime.now(timezone.utc) - timedelta(days=settings["lookback_days"])).date()
    results, errors = [], []
    with tab_api.open_mailbox(source["connection_id"], source["folder"]) as box:
        _, validity_data = box.response("UIDVALIDITY")
        validity = next((v.decode() for v in validity_data or [] if isinstance(v, bytes)), None)
        if not validity or not validity.isdigit():
            raise ValueError("The mail server did not provide UIDVALIDITY")
        status, data = box.uid("search", None, "SINCE", since.strftime("%d-%b-%Y"))
        if status != "OK":
            raise ValueError("Searching the mailbox failed")
        all_ids = (data[0] or b"").split()
        new_ids = [uid for uid in all_ids if f"{validity}:{uid.decode()}" not in known]
        selected = new_ids[:settings["max_messages"]]
        for uid in selected:
            status, info = box.uid("fetch", uid, "(RFC822.SIZE)")
            sizes = re.findall(rb"RFC822.SIZE\s+(\d+)", b" ".join(x for x in info or [] if isinstance(x, bytes)))
            if status != "OK" or not sizes:
                errors.append(f"Could not read size for UID {uid.decode()}")
                continue
            if int(sizes[0]) > MAX_BYTES:
                errors.append(f"UID {uid.decode()} exceeds the 1 MB message limit")
                continue
            status, parts = box.uid("fetch", uid, "(BODY.PEEK[])")
            raw = _literal(parts)
            if status != "OK" or raw is None:
                errors.append(f"Could not read UID {uid.decode()}")
                continue
            msg = email.message_from_bytes(raw)
            mid = (msg.get("Message-ID") or "").strip()
            references = re.findall(r"<[^<>\r\n]+>", msg.get("References", "") or msg.get("In-Reply-To", ""))
            body = text_body(msg)
            context = []
            # Resolve bounded prior replies even when they fall outside the lookback.
            for reference in references[-4:]:
                query = '"' + reference.replace("\\", "\\\\").replace('"', '\\"') + '"'
                ok, found = box.uid("search", None, "HEADER", "Message-ID", query)
                if ok != "OK":
                    continue
                for parent_uid in (found[0] or b"").split()[:1]:
                    ok, parent_size = box.uid("fetch", parent_uid, "(RFC822.SIZE)")
                    sizes = re.findall(rb"RFC822.SIZE\s+(\d+)", b" ".join(x for x in parent_size or [] if isinstance(x, bytes)))
                    if ok != "OK" or not sizes or int(sizes[0]) > MAX_BYTES:
                        continue
                    ok, parent_parts = box.uid("fetch", parent_uid, "(BODY.PEEK[])")
                    parent_raw = _literal(parent_parts)
                    if ok == "OK" and parent_raw:
                        parent = email.message_from_bytes(parent_raw)
                        context.append(f"From: {header(parent.get('From'))}\nDate: {parent.get('Date', '')}\n{text_body(parent)[:3000]}")
            combined = "\n\n".join(context)
            results.append({"external_id": f"{validity}:{uid.decode()}", "message_id": mid,
                "thread_id": references[0] if references else mid or f"{validity}:{uid.decode()}",
                "subject": header(msg.get("Subject")), "sender": parseaddr(header(msg.get("From")))[1],
                "sender_name": parseaddr(header(msg.get("From")))[0], "sent_at": message_time(msg.get("Date")),
                "body": body[:MAX_TEXT], "context": combined[:12000],
                "truncated": len(body) > MAX_TEXT or len(combined) > 12000,
                "context_incomplete": len(context) < min(len(references), 4),
                "url": None, "account": account["email"], "folder": source["folder"]})
    return results, {"remaining": max(0, len(new_ids) - len(selected)), "errors": errors,
                     "window_start": since.isoformat(), "found": len(all_ids)}
