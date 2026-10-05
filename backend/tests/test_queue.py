import unittest
from unittest.mock import Mock

from app.applications.queue import prepare_candidates, refresh_queue
from app.candidate.profile import CandidateProfile


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = CandidateProfile.from_dict({
            "full_name": "Example", "roles": ["Automation Developer"], "countries": ["Europe"],
            "skills": ["n8n"], "work_modes": ["remote"],
        })
        self.rows = [
            {"id": 7, "source": "greenhouse", "board_token": "example", "external_id": "1",
             "company": "Example Ltd", "title": "Automation Developer", "location": "Remote - Europe",
             "description": "Build n8n workflows.", "url": "https://example.com/1", "source_updated_at": None},
            {"id": 8, "source": "greenhouse", "board_token": "example", "external_id": "2",
             "company": "Example Ltd", "title": "Sales Manager", "location": "Remote - Europe",
             "description": "Manage accounts.", "url": "https://example.com/2", "source_updated_at": None},
        ]

    def test_prepares_only_matching_candidates(self) -> None:
        candidates = prepare_candidates(self.profile, self.rows)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].vacancy_id, 7)
        self.assertIn("n8n", candidates[0].draft_text)

    def test_refresh_requires_profile_and_enqueues_once(self) -> None:
        repository = Mock()
        repository.get_profile.return_value = None
        with self.assertRaisesRegex(ValueError, "profile is missing"):
            refresh_queue(repository)
        repository.enqueue_reviews.assert_not_called()
        repository.get_profile.return_value = self.profile
        repository.list_vacancies.return_value = self.rows
        repository.enqueue_reviews.return_value = 1
        self.assertEqual(refresh_queue(repository), {"matching": 1, "added": 1})
        repository.list_vacancies.assert_called_once_with(500)

    def test_aggregated_job_gets_employer_verification_warning(self) -> None:
        row = self.rows[0] | {"source": "himalayas", "location": "Remote - Belarus (Himalayas classification)"}
        candidate = prepare_candidates(self.profile, [row])[0]
        self.assertTrue(any("Himalayas is an aggregator" in warning for warning in candidate.warnings))

    def test_refresh_filters_one_search_track(self) -> None:
        repository = Mock()
        repository.get_profile.return_value = self.profile
        repository.list_vacancies.return_value = [self.rows[0]]
        repository.enqueue_reviews.return_value = 1
        self.assertEqual(refresh_queue(repository, track="international"), {"matching": 1, "added": 1})
        repository.list_vacancies.assert_called_once_with(500, track="international")
        with self.assertRaisesRegex(ValueError, "invalid search track"):
            refresh_queue(repository, track="other")
