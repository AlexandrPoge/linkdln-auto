"""Inspect a saved job-alert email without accessing LinkedIn or the mailbox."""

import re
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from urllib.parse import urlsplit

_MAX_EMAIL_BYTES = 2_000_000
_MAX_LINKS = 200
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_JOB_PATH = re.compile(r"^/(?:comm/)?jobs/view/([^/]+)/?$")
_JOB_ID = re.compile(r"(?:^|-)(\d{7,})$")


@dataclass(frozen=True)
class AlertLink:
    job_id: str
    label: str
    url: str


def _job_link(value: str, label: str = "") -> AlertLink | None:
    try:
        parts = urlsplit(value.strip().rstrip(".,);"))
    except ValueError:
        return None
    if parts.scheme != "https" or parts.netloc.casefold() not in {"linkedin.com", "www.linkedin.com"}:
        return None
    match = _JOB_PATH.fullmatch(parts.path)
    if match is None or (job_id := _JOB_ID.search(match.group(1))) is None:
        return None
    return AlertLink(job_id.group(1), " ".join(label.split())[:200],
                     f"https://www.linkedin.com/jobs/view/{job_id.group(1)}/")


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._label = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._label.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._label)))
            self._href = None
            self._label = []


def parse_alert_email(raw: bytes) -> list[AlertLink]:
    """Return direct job links only; labels and sender identity are unverified."""
    if not raw or len(raw) > _MAX_EMAIL_BYTES:
        raise ValueError("email must contain 1 byte to 2 MB")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    found: dict[str, AlertLink] = {}
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/html", "text/plain"}:
            continue
        try:
            body = part.get_content()
        except (LookupError, UnicodeError, ValueError):
            continue
        if content_type == "text/html":
            extractor = _LinkExtractor()
            extractor.feed(body)
            pairs = extractor.links
        else:
            pairs = [(match.group(), "") for match in _URL_IN_TEXT.finditer(body)]
        for url, label in pairs:
            job = _job_link(url, label)
            if job is not None:
                if job.job_id not in found or (job.label and not found[job.job_id].label):
                    found[job.job_id] = job
                if len(found) > _MAX_LINKS:
                    raise ValueError("email contains too many job links")
    return list(found.values())
