from dataclasses import dataclass
from typing import Any, Protocol

from app.candidate.profile import CandidateProfile
from app.matching.rules import evaluate
from app.templates.drafts import render_application
from app.vacancies.models import SEARCH_TRACKS, Vacancy


@dataclass(frozen=True)
class QueueCandidate:
    vacancy_id: int
    score: int
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    draft_text: str


class QueueRepository(Protocol):
    def get_profile(self) -> CandidateProfile | None: ...
    def list_vacancies(self, limit: int = 20, *, active_only: bool = True,
                       track: str | None = None) -> list[dict[str, Any]]: ...
    def enqueue_reviews(self, candidates: list[QueueCandidate]) -> int: ...


def prepare_candidates(profile: CandidateProfile, rows: list[dict[str, Any]]) -> list[QueueCandidate]:
    candidates: list[QueueCandidate] = []
    for row in rows:
        job = Vacancy(**{field: row[field] for field in Vacancy.__dataclass_fields__ if field in row})
        result = evaluate(profile, job)
        if result.status == "rejected":
            continue
        candidates.append(QueueCandidate(
            vacancy_id=row["id"], score=result.score, reasons=result.reasons,
            warnings=result.warnings, draft_text=render_application(profile, job, result),
        ))
    return candidates


def refresh_queue(repository: QueueRepository, limit: int = 500, *, track: str | None = None) -> dict[str, int]:
    if track is not None and track not in SEARCH_TRACKS:
        raise ValueError("invalid search track")
    profile = repository.get_profile()
    if profile is None:
        raise ValueError("candidate profile is missing; run 'profile set' first")
    rows = repository.list_vacancies(limit) if track is None else repository.list_vacancies(limit, track=track)
    candidates = prepare_candidates(profile, rows)
    return {"matching": len(candidates), "added": repository.enqueue_reviews(candidates)}
