"""Bounded public searches; no authenticated scraping or form submissions."""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from app.integrations.job_sources.text import plain_text
from app.vacancies.models import Vacancy


class PublicSearchError(Exception):
    pass


def get_json(url: str) -> dict:
    request = Request(url, headers={"Accept": "application/json",
                                   "User-Agent": "linkdln-auto/0.2 (personal job search)"})
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read(10_000_001)
    except HTTPError as exc:
        raise PublicSearchError(f"{urlsplit(url).hostname}: HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        raise PublicSearchError(f"{urlsplit(url).hostname}: connection failed") from exc
    if len(raw) > 10_000_000:
        raise PublicSearchError("source response exceeds 10 MB")
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError) as exc:
        raise PublicSearchError("source returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise PublicSearchError("source returned an unexpected response")
    return value


def _required(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PublicSearchError(f"job is missing {name}")
    return value.strip()


def normalize_remotive(item: dict, key: str) -> Vacancy:
    if not isinstance(item, dict):
        raise PublicSearchError("Remotive job must be an object")
    job_id = str(item.get("id", ""))
    url = _required(item.get("url"), "url")
    parts = urlsplit(url)
    if not job_id.isdigit() or parts.scheme != "https" or parts.netloc != "remotive.com":
        raise PublicSearchError("invalid Remotive job identity")
    scope = _required(item.get("candidate_required_location"), "candidate_required_location")
    return Vacancy("remotive", key, job_id, _required(item.get("company_name"), "company"),
                   _required(item.get("title"), "title"), f"Remote - {scope}",
                   plain_text(_required(item.get("description"), "description")), url, None,
                   "belarus" if scope.casefold() == "belarus" else "international")


def fetch_remotive(query: str) -> tuple[str, list[Vacancy]]:
    key = query.strip().casefold()
    data = get_json("https://remotive.com/api/remote-jobs?" + urlencode({"search": query}))
    if not isinstance(data.get("jobs"), list):
        raise PublicSearchError("Remotive returned no jobs list")
    return key, [normalize_remotive(item, key) for item in data["jobs"]]


def normalize_hh(item: dict, key: str) -> Vacancy | None:
    if not isinstance(item, dict):
        raise PublicSearchError("hh job must be an object")
    if item.get("archived"):
        return None
    job_id = str(item.get("id", ""))
    if not job_id.isdigit():
        raise PublicSearchError("invalid hh job identity")
    formats = item.get("work_format") or []
    if not isinstance(formats, list) or any(not isinstance(mode, dict) for mode in formats):
        raise PublicSearchError("invalid hh work format")
    modes = {mode.get("id") for mode in formats}
    # API search filters are not evidence that every returned detail is remote.
    if modes != {"REMOTE"} and not (not modes and (item.get("schedule") or {}).get("id") == "remote"):
        return None
    contacts = item.get("contacts") or {}
    if not isinstance(contacts, dict) or not isinstance(item.get("employer"), dict):
        raise PublicSearchError("invalid hh employer or contacts")
    description = plain_text(_required(item.get("description"), "description"))
    email = contacts.get("email")
    if isinstance(email, str) and email.strip():
        description += "\nApplication email published by employer: " + email.strip()
    return Vacancy("hh", key, job_id, _required((item.get("employer") or {}).get("name"), "employer"),
                   _required(item.get("name"), "name"), "Remote - Belarus (hh search region)",
                   description, f"https://hh.ru/vacancy/{job_id}", None, "belarus")


def fetch_hh(query: str) -> tuple[str, list[Vacancy]]:
    key = "BY:" + query.strip().casefold()
    found: dict[str, Vacancy] = {}
    for page in range(3):
        data = get_json("https://api.hh.ru/vacancies?" + urlencode({
            "text": query, "area": "16", "work_format": "REMOTE", "per_page": 30,
            "page": page, "order_by": "publication_time"}))
        if not isinstance(data.get("items"), list) or not isinstance(data.get("pages"), int):
            raise PublicSearchError("hh returned an unexpected search response")
        for item in data["items"]:
            if not isinstance(item, dict):
                raise PublicSearchError("hh search job must be an object")
            job_id = str(item.get("id", ""))
            if not job_id.isdigit():
                raise PublicSearchError("invalid hh search identity")
            job = normalize_hh(get_json(f"https://api.hh.ru/vacancies/{job_id}"), key)
            if job:
                found[job_id] = job
        if page + 1 >= data["pages"]:
            break
    return key, list(found.values())
