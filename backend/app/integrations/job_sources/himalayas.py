"""Targeted remote-job search via Himalayas' public JSON API."""

import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from app.integrations.job_sources.text import plain_text
from app.vacancies.models import Vacancy

_MAX_RESPONSE_BYTES = 10_000_000
_MAX_PAGES = 10


class HimalayasSourceError(Exception):
    pass


def feed_key(country: str, query: str, *, include_worldwide: bool = False) -> str:
    if not re.fullmatch(r"[A-Za-z]{2}", country):
        raise ValueError("country must be a two-letter ISO code, for example BY")
    if not query.strip() or len(query) > 100:
        raise ValueError("query must contain 1 to 100 characters")
    return f"{country.upper()}:{query.strip().casefold()}:{'all' if include_worldwide else 'explicit'}"


def _himalayas_url(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise HimalayasSourceError(f"Himalayas job has invalid {field}")
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise HimalayasSourceError(f"Himalayas job has invalid {field}") from exc
    if parts.scheme != "https" or parts.netloc.casefold() != "himalayas.app" or not parts.path.startswith("/companies/"):
        raise HimalayasSourceError(f"Himalayas job has invalid {field}")
    return value


def _locations(job: dict[str, Any], country_name: str, include_worldwide: bool) -> tuple[str, str] | None:
    restrictions = job.get("locationRestrictions")
    if not isinstance(restrictions, list):
        raise HimalayasSourceError("Himalayas job has invalid locationRestrictions")
    names: list[str] = []
    for item in restrictions:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        elif isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip():
            names.append(item["name"].strip())
        else:
            raise HimalayasSourceError("Himalayas job has invalid location restriction")
    if any(name.casefold() == country_name.casefold() for name in names):
        if len(names) == 1:
            return f"Remote - {country_name} (Himalayas classification)", "belarus"
        return f"Remote - {country_name} + other countries (Himalayas classification)", "international"
    if not names and include_worldwide:
        return "Remote - Worldwide (Himalayas classification)", "international"
    return None


def normalize_job(job: Any, key: str, country_name: str, *, include_worldwide: bool = False) -> Vacancy | None:
    if not isinstance(job, dict):
        raise HimalayasSourceError("Himalayas job must be an object")
    scope = _locations(job, country_name, include_worldwide)
    if scope is None:
        return None
    location, search_track = scope
    title, company, description = job.get("title"), job.get("companyName"), job.get("description")
    if any(not isinstance(value, str) or not value.strip() for value in (title, company, description)):
        raise HimalayasSourceError("Himalayas job is missing a title, company, or description")
    guid = _himalayas_url(job.get("guid"), "guid")
    link = _himalayas_url(job.get("applicationLink"), "applicationLink")
    return Vacancy(
        source="himalayas", board_token=key, external_id=guid,
        company=company.strip(), title=title.strip(), location=location,
        description=plain_text(description), url=link, source_updated_at=None,
        search_track=search_track,
    )


def _get_page(country: str, query: str, include_worldwide: bool, page: int) -> dict[str, Any]:
    params = {"country": country.upper(), "q": query, "sort": "recent", "page": page}
    if not include_worldwide:
        params["exclude_worldwide"] = "true"
    request = Request(
        "https://himalayas.app/jobs/api/search?" + urlencode(params),
        headers={"Accept": "application/json", "User-Agent": "linkdln-auto/0.1 (personal job search)"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = response.read(_MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise HimalayasSourceError(f"Himalayas request failed: {exc}") from exc
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise HimalayasSourceError("Himalayas response exceeds the size limit")
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HimalayasSourceError("Himalayas returned invalid JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise HimalayasSourceError("Himalayas returned an unexpected search response")
    if not isinstance(data.get("limit"), int) or not 1 <= data["limit"] <= 100:
        raise HimalayasSourceError("Himalayas returned an invalid page limit")
    return data


def fetch_search(country: str, query: str, *, country_name: str,
                 include_worldwide: bool = False) -> tuple[str, list[Vacancy]]:
    key = feed_key(country, query, include_worldwide=include_worldwide)
    if not country_name.strip():
        raise ValueError("country name cannot be empty")
    vacancies: list[Vacancy] = []
    for page in range(1, _MAX_PAGES + 1):
        data = _get_page(country, query, include_worldwide, page)
        page_jobs = data["jobs"]
        if page == 1 and not page_jobs and data.get("totalCount", 0) != 0:
            raise HimalayasSourceError("Himalayas returned an empty first page for a nonempty search")
        vacancies.extend(job for item in page_jobs
                         if (job := normalize_job(item, key, country_name,
                                                  include_worldwide=include_worldwide)) is not None)
        if len(page_jobs) < data["limit"]:
            break
    else:
        raise HimalayasSourceError("Search is too broad; narrow the query to fewer than 10 pages")
    if len({job.external_id for job in vacancies}) != len(vacancies):
        raise HimalayasSourceError("Himalayas returned duplicate jobs across pages")
    return key, vacancies
