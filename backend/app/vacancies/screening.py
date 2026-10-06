"""Screen stored descriptions without sending or modifying user-owned drafts."""

from app.matching.screening import screen
from app.vacancies.models import Vacancy


def evidence(job: Vacancy) -> dict[str, str]:
    return {name: getattr(job, name) for name in Vacancy.__dataclass_fields__ if name != "source_updated_at"}


def screen_saved_vacancies(repository, profile) -> dict[str, int]:
    counts = {"matched": 0, "manual": 0, "rejected": 0}
    after_id = 0
    # Stable ID pagination prevents new boards from hiding older opportunities.
    # Keep the cycle bounded (up to 2,000 descriptions); unchecked rows cannot send.
    for _ in range(10):
        rows = repository.screening_batch(after_id, 200)
        if not rows:
            break
        for row in rows:
            job = Vacancy(**{name: row[name] for name in Vacancy.__dataclass_fields__})
            result = screen(profile, job)
            if repository.save_screening(row["id"], result, evidence(job), profile):
                counts[result.status] += 1
        after_id = rows[-1]["id"]
        if len(rows) < 200:
            break
    return counts
