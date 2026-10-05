import unittest
from unittest.mock import patch

from app.integrations.job_sources.greenhouse import SourceError, fetch_board


def _job(job_id: int, *, title: str = "Engineer") -> dict:
    return {
        "id": job_id,
        "internal_job_id": job_id + 100,
        "title": title,
        "location": {"name": "Remote"},
        "absolute_url": f"https://boards.greenhouse.io/example/jobs/{job_id}",
        "content": "<p>Build APIs &amp; services.</p><script>ignore me</script>",
        "updated_at": "2026-10-01T12:00:00Z",
    }


class GreenhouseTests(unittest.TestCase):
    @patch("app.integrations.job_sources.greenhouse._get_json")
    def test_normalizes_and_deduplicates_board(self, get_json) -> None:
        get_json.side_effect = [
            {"name": "Example Inc"},
            {"jobs": [_job(11), _job(11, title="Senior Engineer"),
                      {"id": 12, "internal_job_id": None}]},
        ]
        vacancies = fetch_board("example")
        self.assertEqual(len(vacancies), 1)
        self.assertEqual(vacancies[0].title, "Senior Engineer")
        self.assertEqual(vacancies[0].description, "Build APIs & services.")
        self.assertEqual(vacancies[0].company, "Example Inc")

    @patch("app.integrations.job_sources.greenhouse._get_json")
    def test_rejects_malformed_feed_before_storage(self, get_json) -> None:
        get_json.side_effect = [{"name": "Example Inc"}, {"jobs": [_job(11), {"id": 13, "internal_job_id": 20}]}]
        with self.assertRaises(SourceError):
            fetch_board("example")

    def test_rejects_board_token_that_could_change_request_path(self) -> None:
        with self.assertRaises(ValueError):
            fetch_board("../other")
