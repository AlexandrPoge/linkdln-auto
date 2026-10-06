"""Local desktop OAuth (PKCE) and Gmail over HTTPS; never uses SMTP."""

import base64
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.applications.delivery import is_email_address
from app.integrations.mailbox import alert_links

SCOPES = ("https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send")
_API = "https://gmail.googleapis.com/gmail/v1/users/me/"
_TOKEN = "https://oauth2.googleapis.com/token"


class GmailAPIError(ValueError):
    """Safe, credential-free error that can be shown on the local page."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward bearer tokens or client secrets to another host.


def _request(url, *, form=None, payload=None, token=None):
    headers = {"Accept": "application/json"}
    body = None
    if form is not None:
        body = urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with build_opener(_NoRedirect()).open(Request(url, data=body, headers=headers), timeout=20) as response:
            raw = response.read(12_000_001)
        if len(raw) > 12_000_000:
            raise GmailAPIError("Gmail API: ответ слишком большой.")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except HTTPError as exc:
        # Do not expose response bodies: they may contain account details or tokens.
        hints = {400: "проверь OAuth-клиент; возможно, доступ истёк — подключи аккаунт заново",
                 401: "подключи аккаунт заново", 403: "включи Gmail API и выдай запрошенные разрешения",
                 429: "превышен лимит Google; повтори позже"}
        raise GmailAPIError(f"Gmail API: HTTP {exc.code}; {hints.get(exc.code, 'проверь доступность Google')}.") from None
    except (URLError, TimeoutError, OSError):
        raise GmailAPIError("Gmail API: нет HTTPS-соединения с Google. Проверь сеть/VPN.") from None
    except (ValueError, UnicodeError):
        raise GmailAPIError("Gmail API: некорректный ответ Google.") from None


def _read(path):
    if not path.exists():
        return {}
    if path.stat().st_size > 1_000_000:
        raise GmailAPIError("Локальный OAuth-файл слишком большой.")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeError):
        raise GmailAPIError("Локальный OAuth-файл повреждён. Подключи аккаунт заново.") from None


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".gmail-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
        os.replace(name, path)  # mkstemp creates an owner-only (0600) file.
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _token_values(value, previous_refresh=""):
    if not isinstance(value.get("access_token"), str) or not value["access_token"] or value.get("token_type", "").lower() != "bearer":
        raise GmailAPIError("Google не вернул действующий access token.")
    expiry = value.get("expires_in")
    if type(expiry) is not int or not 60 <= expiry <= 86400:
        raise GmailAPIError("Google не вернул срок действия токена.")
    refresh = value.get("refresh_token") or previous_refresh
    if not isinstance(refresh, str) or not refresh:
        raise GmailAPIError("Google не выдал фоновый доступ. Подключи аккаунт заново с подтверждением разрешений.")
    return {"access_token": value["access_token"], "refresh_token": refresh, "expires_at": time.time() + expiry}


class GmailOAuth:
    def __init__(self, directory: Path):
        self.client_path = directory / "gmail-client.json"
        self.token_path = directory / "gmail-token.json"
        self.seen_path = directory / "gmail-alerts-seen.json"
        self._lock = threading.RLock()
        self._pending = None

    def status(self):
        value = _read(self.token_path)
        return {"client_configured": self.client_path.is_file(),
                "connected": bool(value.get("refresh_token") and value.get("email")),
                "email": value.get("email", "")}

    def configure(self, raw: str):
        try:
            if len(raw.encode()) > 16000:
                raise ValueError
            value = json.loads(raw)["installed"]
            client_id, secret = value["client_id"], value["client_secret"]
            if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", client_id):
                raise ValueError
            if not isinstance(secret, str) or not 1 <= len(secret) <= 256 or any(c.isspace() for c in secret):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise GmailAPIError("Нужен JSON OAuth-клиента Google типа Desktop app (ключ installed), не API-ключ и не пароль.") from None
        with self._lock:
            if _read(self.token_path) and _read(self.client_path).get("client_id") != client_id:
                raise GmailAPIError("Сначала отключи текущий аккаунт, прежде чем менять OAuth-клиент.")
            _save(self.client_path, {"client_id": client_id, "client_secret": secret})

    def begin(self, port: int, track: str):
        if not 1 <= port <= 65535 or track not in {"belarus", "international"}:
            raise GmailAPIError("Некорректный локальный адрес.")
        with self._lock:
            client = _read(self.client_path)
            if not client:
                raise GmailAPIError("Сначала сохрани JSON OAuth-клиента Google.")
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
            redirect = f"http://127.0.0.1:{port}/gmail-callback"
            self._pending = {"state": state, "verifier": verifier, "redirect": redirect,
                             "expires": time.time() + 600, "track": track, "client": client}
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
                "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
                "scope": " ".join(SCOPES), "state": state, "code_challenge": challenge,
                "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent"})

    def finish(self, state: str, code: str, error: bool = False, on_connected=None):
        with self._lock:
            pending = self._pending
            if not pending or pending["expires"] < time.time() or not secrets.compare_digest(state.encode(), pending["state"].encode()):
                raise GmailAPIError("OAuth-сессия истекла или не совпадает. Начни подключение из панели заново.")
            self._pending = None  # One-use state, including denied/failed callbacks.
            if error or not code or len(code) > 4096:
                raise GmailAPIError("Разрешение Google не получено. Поиск продолжает работать без почты.")
            value = _request(_TOKEN, form={**pending["client"], "code": code,
                "code_verifier": pending["verifier"], "redirect_uri": pending["redirect"],
                "grant_type": "authorization_code"})
            if not set(SCOPES).issubset(set(value.get("scope", "").split())):
                raise GmailAPIError("Нужны оба разрешения: чтение почты и отправка. Подключи аккаунт заново.")
            tokens = _token_values(value)
            account = _request(_API + "profile", token=tokens["access_token"]).get("emailAddress")
            if not isinstance(account, str) or not is_email_address(account) or not account.lower().endswith("@gmail.com"):
                raise GmailAPIError("Подключи личный аккаунт Gmail.")
            tokens["email"] = account.lower()
            if on_connected:
                on_connected(tokens["email"])  # Pause delivery before making the grant visible to workers.
            _save(self.token_path, tokens)
            return pending["track"], tokens["email"]

    def disconnect(self):
        # Local disconnect only; revocation is available in the user's Google account.
        with self._lock:
            self._pending = None
            _save(self.token_path, {})

    def access_token(self):
        with self._lock:
            value = _read(self.token_path)
            if not value.get("refresh_token"):
                raise GmailAPIError("Подключи Google в панели.")
            if value.get("expires_at", 0) <= time.time() + 60:
                result = _request(_TOKEN, form={**_read(self.client_path), "refresh_token": value["refresh_token"],
                                               "grant_type": "refresh_token"})
                value = {**_token_values(result, value["refresh_token"]), "email": value["email"]}
                _save(self.token_path, value)
            return value["access_token"]

    def api(self, path: str, *, query=None, payload=None):
        if not re.fullmatch(r"(?:profile|messages|messages/send|messages/[A-Za-z0-9_-]+)", path):
            raise GmailAPIError("Недопустимый Gmail API endpoint.")
        url = _API + path + ("?" + urlencode(query) if query else "")
        return _request(url, payload=payload, token=self.access_token())

    def send(self, recipient, subject, body, *, filename="", attachment=b""):
        if not is_email_address(recipient):
            raise GmailAPIError("Нужен корректный адрес получателя.")
        message = EmailMessage()
        message["From"] = self.status()["email"]
        message["To"], message["Subject"], message["Message-ID"] = recipient, subject, make_msgid()
        message.set_content(body)
        if attachment:
            message.add_attachment(attachment, maintype="application", subtype="pdf", filename=filename)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        # Never retry POST /send: a lost response may mean the email was delivered.
        result = self.api("messages/send", payload={"raw": raw})
        reference = result.get("id")
        if not isinstance(reference, str) or not reference:
            raise GmailAPIError("Результат отправки неизвестен. Проверь Gmail; не повторяй автоматически.")
        return reference

    def collect_alerts(self, repository):
        account = self.status()["email"]
        receipts = _read(self.seen_path)
        seen = receipts.get("ids", []) if receipts.get("account") == account else []
        if not isinstance(seen, list) or any(not isinstance(item, str) for item in seen):
            raise GmailAPIError("Некорректная история Gmail-уведомлений.")
        known = set(seen)
        added = processed = skipped = 0
        page_token = None
        # Bounded scan (500 IDs, 100 unread-by-this-app messages); no label mutations.
        for _ in range(5):
            query = {"q": "in:inbox newer_than:14d {from:linkedin.com from:hh.ru from:rabota.by}", "maxResults": 100}
            if page_token:
                query["pageToken"] = page_token
            listing = self.api("messages", query=query)
            for item in listing.get("messages", []):
                message_id = item["id"]
                if message_id in known:
                    continue
                value = self.api("messages/" + message_id, query={"format": "raw"})
                raw = value.get("raw", "")
                if value.get("sizeEstimate", 0) > 2_000_000 or len(raw) > 2_700_000:
                    skipped += 1
                else:
                    try:
                        message = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
                        links = alert_links(message)
                    except (ValueError, UnicodeError):
                        skipped += 1
                    else:
                        added += repository.save_alert_leads(links)
                processed += 1
                known.add(message_id)
                seen.append(message_id)
                _save(self.seen_path, {"account": account, "ids": seen[-10000:]})
                if processed >= 100:
                    break
            page_token = listing.get("nextPageToken")
            if processed >= 100 or not page_token:
                break
        return {"processed": processed, "new_links": added, "skipped": skipped}


class GmailApplicationSender:
    channel = "email"  # Share the same once-only identity with the legacy SMTP path.

    def __init__(self, oauth, filename, attachment):
        self.oauth, self.filename, self.attachment = oauth, filename, attachment

    def can_send(self, review):
        return isinstance(review.get("recipient_email"), str) and is_email_address(review["recipient_email"])

    def send(self, review):
        title = " ".join(str(review["title"]).split())[:150]
        company = " ".join(str(review["company"]).split())[:150]
        return self.oauth.send(review["recipient_email"], f"Application: {title} - {company}", review["draft_text"],
                               filename=self.filename, attachment=self.attachment)
