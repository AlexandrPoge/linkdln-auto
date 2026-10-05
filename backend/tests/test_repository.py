import os
import unittest
from datetime import datetime, timezone

import psycopg

from app.candidate.profile import CandidateProfile
from app.infrastructure.persistence.postgres import Repository
from app.vacancies.models import Vacancy


def _vacancy(job_id: str, title: str = "Engineer") -> Vacancy:
    return Vacancy(
        source="greenhouse", board_token="integrationtest", external_id=job_id,
        company="Example Inc", title=title, location="Remote", description="Build APIs",
        url=f"https://example.com/jobs/{job_id}",
        source_updated_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not set")
class RepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        url = os.environ["TEST_DATABASE_URL"]
        if not psycopg.conninfo.conninfo_to_dict(url).get("dbname", "").endswith("_test"):
            raise RuntimeError("TEST_DATABASE_URL must point to a database ending in _test")
        cls.repository = Repository(url)
        cls.repository.initialize()

    def setUp(self) -> None:
        with psycopg.connect(self.repository.database_url) as connection:
            connection.execute("TRUNCATE vacancies, candidate_profiles")

    def test_profile_round_trip(self) -> None:
        profile = CandidateProfile.from_dict({
            "full_name": "Ada", "resume_text": "Built systems",
            "roles": ["Engineer"], "countries": ["UK"],
            "skills": ["Python"], "work_modes": ["remote"],
        })
        self.repository.save_profile(profile)
        self.assertEqual(self.repository.get_profile(), profile)

    def test_import_updates_without_duplicates_and_closes_missing_jobs(self) -> None:
        first = self.repository.import_board("integrationtest", [_vacancy("1"), _vacancy("2")])
        self.assertEqual(first, {"total": 2, "new": 2, "closed": 0})
        second = self.repository.import_board("integrationtest", [_vacancy("1", "Senior Engineer")])
        self.assertEqual(second, {"total": 1, "new": 0, "closed": 1})
        active = self.repository.list_vacancies()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["title"], "Senior Engineer")
        self.assertEqual(len(self.repository.list_vacancies(active_only=False)), 2)
        third = self.repository.import_board("integrationtest", [_vacancy("1"), _vacancy("2")])
        self.assertEqual(third, {"total": 2, "new": 0, "closed": 0})
        self.assertEqual(len(self.repository.list_vacancies()), 2)
