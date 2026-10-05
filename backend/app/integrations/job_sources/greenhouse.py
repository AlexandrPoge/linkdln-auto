import json
import re
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from app.vacancies.models import Vacancy

_BOARD_TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_RESPONSE_BYTES = 20_000_000


class SourceError(Exception):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden_depth += 1
        elif tag in {"p", "br", "li", "div", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif tag in {"p", "li", "div", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


def _plain_text(content: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(unescape(content))
    return " ".join(" ".join(extractor.parts).split())


def _get_json(url: str) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "linkdln-auto/0.1 (personal job search)"})
    try:
        with urlopen(request, timeout=15) as response:
            payload = response.read(_MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise SourceError(f"Greenhouse request failed: {exc}") from exc
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise SourceError("Greenhouse response exceeds the size limit")
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceError("Greenhouse returned invalid JSON") from exc


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceError(f"Greenhouse job has invalid {field}")
    return value.strip()


def _normalize_job(job: Any, board_token: str, company: str) -> Vacancy | None:
    if not isinstance(job, dict):
        raise SourceError("Greenhouse job must be an object")
    if "internal_job_id" not in job:
        raise SourceError("Greenhouse job is missing internal_job_id")
    if job.get("internal_job_id") is None:
        return None  # Prospect posts are not concrete vacancies.
    job_id = job.get("id")
    if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id <= 0:
        raise SourceError("Greenhouse job has invalid id")
    url = _required_text(job.get("absolute_url"), "absolute_url")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise SourceError("Greenhouse job has invalid absolute_url") from exc
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise SourceError("Greenhouse job has invalid absolute_url")
    location = job.get("location")
    if not isinstance(location, dict):
        raise SourceError("Greenhouse job has invalid location")
    location_name = _required_text(location.get("name"), "location.name")
    content = job.get("content")
    if content is not None and not isinstance(content, str):
        raise SourceError("Greenhouse job has invalid content")
    updated_at = job.get("updated_at")
    if updated_at is not None:
        try:
            updated_at = datetime.fromisoformat(_required_text(updated_at, "updated_at").replace("Z", "+00:00"))
        except ValueError as exc:
            raise SourceError("Greenhouse job has invalid updated_at") from exc
        if updated_at.tzinfo is None:
            raise SourceError("Greenhouse job updated_at must include a timezone")
    return Vacancy(
        source="greenhouse",
        board_token=board_token,
        external_id=str(job_id),
        company=company,
        title=_required_text(job.get("title"), "title"),
        location=location_name,
        description=_plain_text(content or ""),
        url=url,
        source_updated_at=updated_at,
    )


def fetch_board(board_token: str) -> list[Vacancy]:
    if not _BOARD_TOKEN.fullmatch(board_token):
        raise ValueError("board token must contain only letters, digits, '-' or '_'")
    base = f"https://boards-api.greenhouse.io/v1/boards/{board_token}"
    board = _get_json(base)
    listing = _get_json(f"{base}/jobs?content=true")
    if not isinstance(board, dict) or not isinstance(listing, dict) or not isinstance(listing.get("jobs"), list):
        raise SourceError("Greenhouse returned an unexpected board or jobs response")
    company = _required_text(board.get("name"), "board.name")
    vacancies = [_normalize_job(job, board_token, company) for job in listing["jobs"]]
    return list({job.external_id: job for job in vacancies if job is not None}.values())
