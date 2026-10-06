"""Read job-alert messages using TLS/IMAP without modifying the mailbox."""

import hashlib
import imaplib
import re
import ssl
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from urllib.parse import urlsplit

from app.integrations.job_sources.linkedin_alert import (
    _LinkExtractor, _URL_IN_TEXT, parse_alert_email,
)


def alert_links(raw: bytes) -> list[dict]:
    if not raw or len(raw) > 2_000_000:
        raise ValueError("email must contain 1 byte to 2 MB")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    sender = parseaddr(str(message.get("From", "")))[1].split("@")[-1].lower()
    if not any(sender == domain or sender.endswith("." + domain)
               for domain in ("linkedin.com", "hh.ru", "rabota.by")):
        return []
    result = {("linkedin", link.job_id): {"source": "linkedin", "external_id": link.job_id,
              "title": link.label or "LinkedIn: ссылка из уведомления", "url": link.url}
              for link in parse_alert_email(raw)}
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() not in {"text/html", "text/plain"}:
            continue
        try:
            body = part.get_content()
        except (LookupError, UnicodeError, ValueError):
            continue
        if part.get_content_type() == "text/html":
            extractor = _LinkExtractor()
            extractor.feed(body)
            pairs = extractor.links
        else:
            pairs = [(match.group(), "") for match in _URL_IN_TEXT.finditer(body)]
        for url, label in pairs:
            try:
                parts = urlsplit(url)
            except ValueError:
                continue
            match = re.fullmatch(r"/vacancy/(\d+)/?", parts.path)
            if parts.scheme != "https" or parts.netloc.lower() not in {"hh.ru", "www.hh.ru", "rabota.by", "www.rabota.by"} or not match:
                continue
            job_id = match.group(1)
            result[("hh", job_id)] = {"source": "hh", "external_id": job_id,
                "title": " ".join(label.split())[:200] or "hh: ссылка из уведомления",
                "url": f"https://hh.ru/vacancy/{job_id}"}
    if len(result) > 200:
        raise ValueError("too many links in one message")
    return list(result.values())


def verify_gmail(email: str, password: str) -> None:
    import smtplib
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20, context=context) as smtp:
        smtp.login(email, password)
    with imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=context, timeout=20) as mailbox:
        mailbox.login(email, password)


def collect_alerts(repository, email: str, password: str) -> dict[str, int]:
    account = hashlib.sha256(email.lower().encode()).hexdigest()
    added = processed = skipped = 0
    with imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=ssl.create_default_context(), timeout=20) as mailbox:
        mailbox.login(email, password)
        if mailbox.select("INBOX", readonly=True)[0] != "OK":
            raise ValueError("cannot read inbox")
        validity = mailbox.response("UIDVALIDITY")[1]
        if not validity or not validity[0] or not validity[0].isdigit():
            raise ValueError("mailbox has no stable UIDVALIDITY")
        epoch = validity[0].decode("ascii")
        last_uid = repository.mailbox_cursor(account, epoch)
        since = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%d-%b-%Y")
        status, values = mailbox.uid("search", None,
            f'(UID {last_uid + 1}:* SINCE {since} OR FROM "linkedin.com" OR FROM "hh.ru" FROM "rabota.by")')
        if status != "OK" or not values or not isinstance(values[0], bytes):
            raise ValueError("job-alert search failed")
        # IMAP n:* can return n-1 when n exceeds the highest UID.
        uids = sorted({int(uid) for uid in values[0].split() if uid.isdigit() and int(uid) > last_uid})[:100]
        for uid in uids:
            status, size_parts = mailbox.uid("fetch", str(uid), "(RFC822.SIZE)")
            size_info = b" ".join(item for item in size_parts if isinstance(item, bytes)) if size_parts else b""
            size = re.search(rb"RFC822.SIZE (\d+)", size_info)
            if status != "OK" or not size:
                raise ValueError("job-alert size request failed")
            if int(size.group(1)) > 2_000_000:
                skipped += 1
            else:
                status, parts = mailbox.uid("fetch", str(uid), "(BODY.PEEK[])")
                raw = next((item[1] for item in parts if isinstance(item, tuple) and isinstance(item[1], bytes)), None)
                if status != "OK" or raw is None:
                    raise ValueError("job-alert fetch failed")
                try:
                    links = alert_links(raw)
                except (ValueError, UnicodeError):
                    skipped += 1
                else:
                    added += repository.save_alert_leads(links)
            repository.save_mailbox_cursor(account, epoch, uid)
            processed += 1
    return {"processed": processed, "new_links": added, "skipped": skipped}
