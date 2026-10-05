import json
import unittest
from unittest.mock import patch

from app.candidate.profile import CandidateProfile
from app.integrations.job_sources.ashby import AshbySourceError, fetch_board, normalize_job
from app.matching.rules import evaluate


def _job(job_id: str = "job-1", **overrides: object) -> dict:
    return {
        "title": "AI Automation Engineer",
        "location": "Berlin Office",
        "secondaryLocations": [{"location": "Germany"}, {"location": "Poland"}],
        "isListed": True,
        "isRemote": True,
        "workplaceType": "Remote",
        "descriptionPlain": "Build n8n API integrations.",
        "jobUrl": f"https://jobs.ashbyhq.com/example/{job_id}",
    } | overrides


class AshbyTests(unittest.TestCase):
    def test_normalizes_remote_countries_without_claiming_worldwide_eligibility(self) -> None:
        vacancy = normalize_job(_job(), "example", "Example Inc")
        self.assertIsNotNone(vacancy)
        self.assertEqual(vacancy.external_id, "job-1")
        self.assertEqual(vacancy.location, "Remote - Berlin Office; Remote - Germany; Remote - Poland")
        profile = CandidateProfile.from_dict({
            "full_name": "Ada", "resume_text": "", "roles": ["AI Automation Engineer"],
            "skills": ["n8n"], "countries": ["Europe"], "work_modes": ["remote"],
            "residence_country": "Belarus",
        })
        self.assertEqual(evaluate(profile, vacancy).status, "rejected")

    def test_skips_unlisted_jobs_and_keeps_hybrid_restriction(self) -> None:
        self.assertIsNone(normalize_job(_job(isListed=False), "example", "Example Inc"))
        vacancy = normalize_job(_job(workplaceType="Hybrid"), "example", "Example Inc")
        self.assertTrue(vacancy.location.startswith("Hybrid -"))

    @patch("app.integrations.job_sources.ashby.urlopen")
    def test_fetches_public_board_and_rejects_malformed_jobs(self, urlopen) -> None:
        class Response:
            def __init__(self, data: bytes) -> None:
                self.data = data

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _limit):
                return self.data

        urlopen.return_value = Response(json.dumps({
            "apiVersion": "1", "jobs": [_job(), _job("job-2", isListed=False)],
        }).encode())
        vacancies = fetch_board("example", company="Example Inc")
        self.assertEqual(len(vacancies), 1)
        self.assertEqual(vacancies[0].company, "Example Inc")
        self.assertEqual(vacancies[0].source, "ashby")

        urlopen.return_value = Response(json.dumps({"apiVersion": "1", "jobs": [_job(), _job("job-1")]}).encode())
        with self.assertRaisesRegex(AshbySourceError, "duplicate"):
            fetch_board("example")

    def test_rejects_wrong_board_or_invalid_token(self) -> None:
        with self.assertRaises(AshbySourceError):
            normalize_job(_job(jobUrl="https://jobs.ashbyhq.com/other/job-1"), "example", "Example Inc")
        with self.assertRaises(ValueError):
            fetch_board("../other")


if __name__ == "__main__":
    unittest.main()
