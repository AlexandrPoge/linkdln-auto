import unittest
from unittest.mock import patch

from app.integrations.job_sources.public_search import (
    PublicSearchError, fetch_hh, fetch_hh_detail, fetch_remotive, normalize_hh, normalize_remotive,
)
from app.vacancies.sync import parse_search_plan


class PublicSearchTests(unittest.TestCase):
    def test_remotive_preserves_geography_and_source_link(self):
        job = normalize_remotive({"id": 1, "url": "https://remotive.com/remote-jobs/software-dev/1",
            "company_name": "Example", "title": "Automation Engineer", "description": "<p>n8n</p>",
            "candidate_required_location": "USA"}, "automation")
        self.assertEqual(job.location, "Remote - USA")
        self.assertEqual(job.description, "n8n")
        with self.assertRaises(PublicSearchError):
            normalize_remotive([], "automation")

    def test_hh_does_not_treat_search_filter_as_remote_confirmation(self):
        detail = {"id": "123", "employer": {"name": "Example"}, "name": "Инженер по автоматизации",
                  "description": "n8n", "work_format": [{"id": "HYBRID"}]}
        self.assertIsNone(normalize_hh(detail, "BY:n8n"))
        job = normalize_hh(detail | {"work_format": [{"id": "REMOTE"}],
                                    "contacts": {"email": "jobs@example.com"}}, "BY:n8n")
        self.assertEqual(job.search_track, "belarus")
        self.assertIn("Application email published by employer", job.description)
        self.assertIn("hiring scope unconfirmed", job.location)
        self.assertNotIn("Remote - Belarus", job.location)

    @patch("app.integrations.job_sources.public_search.get_json")
    def test_hh_detail_rejects_invalid_or_mismatched_id(self, get):
        for identity in ("../profile", "1?redirect=localhost", "https://evil.test", "1" * 21):
            with self.subTest(identity=identity), self.assertRaises(PublicSearchError):
                fetch_hh_detail(identity)
        get.assert_not_called()
        get.return_value = {"id": "2"}
        with self.assertRaisesRegex(PublicSearchError, "different vacancy"):
            fetch_hh_detail("1")

    def test_public_api_never_follows_redirects(self):
        from app.integrations.job_sources.public_search import _NoRedirect
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "http://127.0.0.1/"))

    @patch("app.integrations.job_sources.public_search.get_json")
    def test_hh_uses_fixed_public_endpoint_and_remote_by_scope(self, get):
        get.side_effect = [{"pages": 1, "items": [{"id": "123"}]},
            {"id": "123", "employer": {"name": "Example"}, "name": "Automation Engineer",
             "description": "n8n", "work_format": [{"id": "REMOTE"}]}]
        key, jobs = fetch_hh("n8n")
        self.assertEqual(key, "BY:n8n")
        self.assertEqual(len(jobs), 1)
        self.assertIn("area=16", get.call_args_list[0].args[0])
        self.assertIn("work_format=REMOTE", get.call_args_list[0].args[0])

    def test_config_supports_new_sources_and_caps_remotive_requests(self):
        plan = parse_search_plan({"sources": [{"source": "remotive", "query": "automation"},
            {"source": "hh", "query": "n8n"},
            {"source": "himalayas", "country": "BY", "query": "n8n", "include_worldwide": True}]})
        self.assertEqual(len(plan), 3)
        with self.assertRaises(ValueError):
            parse_search_plan({"sources": [{"source": "remotive", "query": "a"},
                                          {"source": "remotive", "query": "b"}]})


if __name__ == "__main__":
    unittest.main()
