"""Screen stored descriptions without sending or modifying user-owned drafts."""

from app.matching.screening import screen
from app.vacancies.models import Vacancy


def evidence(job: Vacancy) -> dict[str, str]:
    return {name: getattr(job, name) for name in Vacancy.__dataclass_fields__ if name != "source_updated_at"}


def screen_saved_vacancies(repository, profile) -> dict[str, int]:
    counts = {"matched": 0, "manual": 0, "rejected": 0}
    for row in repository.list_vacancies(500):
        job = Vacancy(**{name: row[name] for name in Vacancy.__dataclass_fields__})
        result = screen(profile, job)
        if repository.save_screening(row["id"], result, evidence(job), profile):
            counts[result.status] += 1
    return counts
