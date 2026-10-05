"""One-shot, repeatable search across configured public job sources."""

import re
from dataclasses import dataclass
from typing import Any, Protocol

from app.applications.queue import refresh_queue
from app.candidate.profile import CandidateProfile
from app.integrations.job_sources.ashby import AshbySourceError, fetch_board as fetch_ashby_board
from app.integrations.job_sources.greenhouse import SourceError, fetch_board as fetch_greenhouse_board
from app.integrations.job_sources.himalayas import HimalayasSourceError, fetch_search
from app.vacancies.models import Vacancy

_BOARD_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_MAX_SOURCES = 20


class SearchRepository(Protocol):
    def get_profile(self) -> CandidateProfile | None: ...
    def import_board(self, board_token: str, vacancies: list[Vacancy], *, source: str,
                     close_missing: bool = True) -> dict[str, int]: ...
    def list_vacancies(self, limit: int = 20, *, active_only: bool = True,
                       track: str | None = None) -> list[dict[str, Any]]: ...
    def enqueue_reviews(self, candidates: list[Any]) -> int: ...


@dataclass(frozen=True)
class SearchSource:
    source: str
    board_token: str | None = None
    company: str | None = None
    country: str | None = None
    query: str | None = None

    @property
    def name(self) -> str:
        if self.source == "himalayas":
            return f"himalayas:{self.country}:{self.query}"
        return f"{self.source}:{self.board_token}"


def parse_search_plan(value: Any) -> tuple[SearchSource, ...]:
    if not isinstance(value, dict) or set(value) != {"sources"}:
        raise ValueError("search config must contain only a sources list")
    sources = value["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= _MAX_SOURCES:
        raise ValueError("search config requires 1 to 20 sources")
    result: list[SearchSource] = []
    for entry in sources:
        if not isinstance(entry, dict):
            raise ValueError("each source must be an object")
        source = entry.get("source")
        if isinstance(source, str) and source in {"greenhouse", "ashby"}:
            allowed = {"source", "board_token"} | ({"company"} if source == "ashby" else set())
            token = entry.get("board_token")
            company = entry.get("company")
            if set(entry) - allowed or not isinstance(token, str) or not _BOARD_TOKEN.fullmatch(token):
                raise ValueError(f"invalid {source} board settings")
            if company is not None and (not isinstance(company, str) or not company.strip() or len(company) > 100):
                raise ValueError("company must contain 1 to 100 characters")
            result.append(SearchSource(source, board_token=token, company=company.strip() if company else None))
        elif source == "himalayas":
            country, query = entry.get("country"), entry.get("query")
            if set(entry) != {"source", "country", "query"} or country != "BY" or \
                    not isinstance(query, str) or not 1 <= len(query.strip()) <= 100:
                raise ValueError("Himalayas search requires country BY and a query of 1 to 100 characters")
            result.append(SearchSource(source, country="BY", query=query.strip()))
        else:
            raise ValueError("unsupported search source")
    names = [item.name.casefold() for item in result]
    if len(names) != len(set(names)):
        raise ValueError("duplicate search sources are not allowed")
    return tuple(result)


def run_searches(repository: SearchRepository, sources: tuple[SearchSource, ...]) -> dict[str, Any]:
    profile = repository.get_profile()
    if profile is None:
        raise ValueError("candidate profile is missing; run 'profile set' first")
    if not sources:
        raise ValueError("at least one search source is required")
    results: list[dict[str, Any]] = []
    failed = 0
    successful = 0
    for item in sources:
        try:
            if item.source == "greenhouse":
                assert item.board_token is not None
                jobs = fetch_greenhouse_board(item.board_token)
                imported = repository.import_board(item.board_token, jobs, source="greenhouse")
            elif item.source == "ashby":
                assert item.board_token is not None
                jobs = fetch_ashby_board(item.board_token, company=item.company)
                imported = repository.import_board(item.board_token, jobs, source="ashby")
            elif item.source == "himalayas":
                if profile.residence_country != "Belarus":
                    raise ValueError("Himalayas BY search requires residence_country Belarus")
                assert item.query is not None
                key, jobs = fetch_search("BY", item.query, country_name="Belarus")
                imported = repository.import_board(key, jobs, source="himalayas", close_missing=False)
            else:
                raise ValueError("unsupported search source")
            successful += 1
            results.append({"source": item.name, "status": "ok", **imported})
        except (SourceError, AshbySourceError, HimalayasSourceError, ValueError) as exc:
            failed += 1
            results.append({"source": item.name, "status": "failed", "error": str(exc)})
    queue = ({track: refresh_queue(repository, track=track) for track in ("belarus", "international")}
             if successful else None)
    return {"sources": results, "successful": successful, "failed": failed, "queue": queue,
            "sent": 0}
