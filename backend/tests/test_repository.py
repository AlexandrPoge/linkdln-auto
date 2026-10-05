import os
import unittest
from datetime import datetime, timezone
from dataclasses import replace

import psycopg

from app.candidate.profile import CandidateProfile
from app.applications.queue import QueueCandidate
from app.infrastructure.persistence.postgres import Repository
from app.vacancies.models import Vacancy


def _vacancy(job_id: str, title: str = "Engineer") -> Vacancy:
    return Vacancy(
        source="greenhouse", board_token="integrationtest", external_id=job_id,
        company="Example Inc", title=title, location="Remote", description="Build APIs",
        url=f"https://example.com/jobs/{job_id}",
        source_updated_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )


def _ashby_vacancy(job_id: str) -> Vacancy:
    return Vacancy(
        source="ashby", board_token="integrationtest", external_id=job_id,
        company="Example Inc", title="Automation Developer", location="Remote - Europe",
        description="Build API integrations", url=f"https://jobs.ashbyhq.com/integrationtest/{job_id}",
        source_updated_at=None,
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
            connection.execute("TRUNCATE application_delivery_attempts, application_reviews, vacancies, candidate_profiles RESTART IDENTITY")

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

    def test_sources_with_the_same_board_token_do_not_close_each_other(self) -> None:
        self.repository.import_board("integrationtest", [_vacancy("1")])
        result = self.repository.import_board("integrationtest", [_ashby_vacancy("1")], source="ashby")
        self.assertEqual(result, {"total": 1, "new": 1, "closed": 0})
        self.assertEqual({row["source"] for row in self.repository.list_vacancies()}, {"greenhouse", "ashby"})
        self.repository.import_board("integrationtest", [], source="ashby")
        self.assertEqual([row["source"] for row in self.repository.list_vacancies()], ["greenhouse"])

    def test_search_import_does_not_close_jobs_missing_from_later_results(self) -> None:
        first = [_vacancy("1"), _vacancy("2")]
        first = [Vacancy(source="himalayas", board_token="BY:automation:explicit", **{
            field: getattr(job, field) for field in Vacancy.__dataclass_fields__
            if field not in {"source", "board_token"}}) for job in first]
        self.repository.import_board("BY:automation:explicit", first, source="himalayas", close_missing=False)
        result = self.repository.import_board("BY:automation:explicit", first[:1], source="himalayas",
                                              close_missing=False)
        self.assertEqual(result["closed"], 0)
        self.assertEqual(len(self.repository.list_vacancies()), 2)

    def test_tracks_filter_vacancies_and_review_queue(self) -> None:
        self.repository.import_board("integrationtest", [
            replace(_vacancy("by"), location="Remote - Belarus", search_track="belarus"),
            replace(_vacancy("world"), location="Remote - Europe", search_track="international"),
        ])
        belarus = self.repository.list_vacancies(track="belarus")
        abroad = self.repository.list_vacancies(track="international")
        self.assertEqual([row["external_id"] for row in belarus], ["by"])
        self.assertEqual([row["external_id"] for row in abroad], ["world"])
        self.repository.enqueue_reviews([
            QueueCandidate(row["id"], 60, (), (), "Draft") for row in belarus + abroad
        ])
        self.assertEqual([row["title"] for row in self.repository.list_reviews(track="belarus")], ["Engineer"])
        self.assertEqual([row["search_track"] for row in self.repository.list_reviews(track="international")],
                         ["international"])
        with self.assertRaisesRegex(ValueError, "invalid search track"):
            self.repository.list_reviews(track="invalid")

    def test_queue_preserves_edits_and_approval_is_not_a_send(self) -> None:
        self.repository.import_board("integrationtest", [_vacancy("1"), _vacancy("2")])
        rows = self.repository.list_vacancies()
        candidates = [QueueCandidate(row["id"], 75, ("Role matched",), ("Check eligibility",), "Original draft")
                      for row in rows]
        self.assertEqual(self.repository.enqueue_reviews(candidates), 2)
        first_id, second_id = [row["id"] for row in self.repository.list_reviews()]
        self.assertTrue(self.repository.edit_review(first_id, "Edited draft"))
        self.assertEqual(self.repository.enqueue_reviews(candidates), 0)
        self.assertEqual(self.repository.list_reviews()[0]["draft_text"], "Edited draft")
        with self.assertRaisesRegex(ValueError, "active draft"):
            self.repository.approve_reviews([first_id, 999999])
        self.assertEqual([row["status"] for row in self.repository.list_reviews()], ["draft", "draft"])
        self.assertEqual(self.repository.approve_reviews([first_id, second_id]), 2)
        self.assertFalse(self.repository.edit_review(first_id, "No longer editable"))
        self.assertTrue(all(row["status"] == "approved" for row in self.repository.list_reviews()))

    def test_closed_vacancy_cannot_be_approved(self) -> None:
        self.repository.import_board("integrationtest", [_vacancy("1")])
        vacancy_id = self.repository.list_vacancies()[0]["id"]
        self.repository.enqueue_reviews([QueueCandidate(vacancy_id, 60, (), (), "Draft")])
        review_id = self.repository.list_reviews()[0]["id"]
        self.repository.import_board("integrationtest", [])
        with self.assertRaisesRegex(ValueError, "active draft"):
            self.repository.approve_reviews([review_id])

    def test_rejected_review_can_be_restored(self) -> None:
        self.repository.import_board("integrationtest", [_vacancy("1")])
        vacancy_id = self.repository.list_vacancies()[0]["id"]
        self.repository.enqueue_reviews([QueueCandidate(vacancy_id, 60, (), (), "Draft")])
        review_id = self.repository.list_reviews()[0]["id"]
        self.assertTrue(self.repository.reject_review(review_id))
        self.assertEqual(self.repository.list_reviews()[0]["status"], "rejected")
        self.assertTrue(self.repository.restore_review(review_id))
        self.assertEqual(self.repository.list_reviews()[0]["status"], "draft")

    def test_delivery_claim_is_once_only_and_requires_approval(self) -> None:
        self.repository.import_board("integrationtest", [_vacancy("1")])
        vacancy_id = self.repository.list_vacancies()[0]["id"]
        self.repository.enqueue_reviews([QueueCandidate(vacancy_id, 60, (), (), "Draft")])
        review_id = self.repository.list_reviews()[0]["id"]
        self.assertIsNone(self.repository.claim_delivery(review_id, "email"))
        self.repository.approve_reviews([review_id])
        self.assertTrue(self.repository.set_review_recipient(review_id, "jobs@example.com"))
        with self.assertRaisesRegex(ValueError, "valid employer email"):
            self.repository.set_review_recipient(review_id, "bad-address")
        self.assertEqual([row["id"] for row in self.repository.list_sendable_reviews()], [review_id])
        attempt_id = self.repository.claim_delivery(review_id, "email", "jobs@example.com")
        self.assertIsInstance(attempt_id, int)
        self.assertFalse(self.repository.set_review_recipient(review_id, "other@example.com"))
        self.assertEqual(self.repository.list_reviews()[0]["recipient_email"], "jobs@example.com")
        self.assertIsNone(self.repository.claim_delivery(review_id, "email"))
        self.assertEqual(self.repository.list_sendable_reviews(), [])
        self.repository.finish_delivery(attempt_id, sent=False, error="outcome unknown")
        self.assertEqual(self.repository.list_reviews()[0]["delivery_status"], "uncertain")
        with self.assertRaisesRegex(ValueError, "already finalized"):
            self.repository.finish_delivery(attempt_id, sent=True)

    def test_same_source_job_from_two_queries_cannot_be_delivered_twice(self) -> None:
        first = replace(_vacancy("duplicate"), source="himalayas", board_token="BY:one:explicit")
        second = replace(first, board_token="BY:two:explicit")
        self.repository.import_board(first.board_token, [first], source="himalayas", close_missing=False)
        self.repository.import_board(second.board_token, [second], source="himalayas", close_missing=False)
        rows = self.repository.list_vacancies()
        self.repository.enqueue_reviews([QueueCandidate(row["id"], 60, (), (), "Draft") for row in rows])
        review_ids = [row["id"] for row in self.repository.list_reviews()]
        self.repository.approve_reviews(review_ids)
        self.assertIsInstance(self.repository.claim_delivery(review_ids[0], "email"), int)
        self.assertIsNone(self.repository.claim_delivery(review_ids[1], "email"))
