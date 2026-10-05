"""Read listed vacancies from Ashby's public Job Postings API."""

import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from app.vacancies.models import Vacancy, track_from_location

_BOARD_TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_RESPONSE_BYTES = 20_000_000


class AshbySourceError(Exception):
    pass


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AshbySourceError(f"Ashby job has invalid {field}")
    return value.strip()


def _job_identity(url: str, board_token: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise AshbySourceError("Ashby job has invalid jobUrl") from exc
    path = parts.path.strip("/").split("/")
    if parts.scheme != "https" or parts.netloc.casefold() != "jobs.ashbyhq.com":
        raise AshbySourceError("Ashby jobUrl must use the public Ashby job board")
    if len(path) != 2 or path[0].casefold() != board_token.casefold() or not path[1]:
        raise AshbySourceError("Ashby jobUrl does not belong to the requested board")
    return path[1]


def _location(job: dict[str, Any]) -> str:
    primary = _required_text(job.get("location"), "location")
    secondary = job.get("secondaryLocations", [])
    if not isinstance(secondary, list):
        raise AshbySourceError("Ashby job has invalid secondaryLocations")
    locations = [primary]
    for entry in secondary:
        if not isinstance(entry, dict):
            raise AshbySourceError("Ashby job has invalid secondaryLocations entry")
        locations.append(_required_text(entry.get("location"), "secondaryLocations.location"))
    workplace = job.get("workplaceType")
    is_remote = job.get("isRemote")
    if workplace not in {"Remote", "Hybrid", "OnSite"} or not isinstance(is_remote, bool):
        raise AshbySourceError("Ashby job has invalid workplace type")
    if workplace == "Remote" and not is_remote:
        raise AshbySourceError("Ashby job has contradictory remote fields")
    if workplace != "Remote" and any(re.search(r"\bremote\b", location, re.IGNORECASE) for location in locations):
        raise AshbySourceError("Ashby job has contradictory workplace and location fields")
    # Preserve every stated location: a remote role may still allow only named countries.
    label = "Remote" if workplace == "Remote" else "Hybrid" if workplace == "Hybrid" else "On-site"
    return "; ".join(
        location if re.search(r"\b(remote|hybrid|on[ -]?site)\b", location, re.IGNORECASE)
        else f"{label} - {location}"
        for location in dict.fromkeys(locations)
    )


def normalize_job(job: Any, board_token: str, company: str) -> Vacancy | None:
    if not isinstance(job, dict):
        raise AshbySourceError("Ashby job must be an object")
    listed = job.get("isListed")
    if not isinstance(listed, bool):
        raise AshbySourceError("Ashby job has invalid isListed")
    if not listed:
        return None
    url = _required_text(job.get("jobUrl"), "jobUrl")
    external_id = _job_identity(url, board_token)
    description = job.get("descriptionPlain")
    if not isinstance(description, str):
        raise AshbySourceError("Ashby job has invalid descriptionPlain")
    location = _location(job)
    return Vacancy(
        source="ashby", board_token=board_token, external_id=external_id,
        company=company, title=_required_text(job.get("title"), "title"),
        location=location, description=description.strip(),
        url=url, source_updated_at=None, search_track=track_from_location(location),
    )


def fetch_board(board_token: str, *, company: str | None = None) -> list[Vacancy]:
    if not _BOARD_TOKEN.fullmatch(board_token):
        raise ValueError("board token must contain only letters, digits, '-' or '_'")
    if company is not None and not company.strip():
        raise ValueError("company name cannot be empty")
    request = Request(
        f"https://api.ashbyhq.com/posting-api/job-board/{board_token}",
        headers={"Accept": "application/json", "User-Agent": "linkdln-auto/0.1 (personal job search)"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = response.read(_MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise AshbySourceError(f"Ashby request failed: {exc}") from exc
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise AshbySourceError("Ashby response exceeds the size limit")
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AshbySourceError("Ashby returned invalid JSON") from exc
    if not isinstance(data, dict) or data.get("apiVersion") != "1" or not isinstance(data.get("jobs"), list):
        raise AshbySourceError("Ashby returned an unexpected jobs response")
    jobs = [normalize_job(job, board_token, company or board_token) for job in data["jobs"]]
    result = [job for job in jobs if job is not None]
    if len({job.external_id for job in result}) != len(result):
        raise AshbySourceError("Ashby board contains duplicate job ids")
    return result
