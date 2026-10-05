import json
import unittest
from unittest.mock import patch

from app.integrations.job_sources.himalayas import HimalayasSourceError, feed_key, fetch_search, normalize_job


def _job(job_id: str, restrictions: list[object]) -> dict:
    link = f"https://himalayas.app/companies/example/jobs/{job_id}"
    return {
        "guid": link, "applicationLink": link, "title": "AI Automation Engineer",
        "companyName": "Example", "description": "<p>Build n8n &amp; API workflows.</p>",
        "locationRestrictions": restrictions,
    }


class HimalayasTests(unittest.TestCase):
    def test_explicit_country_is_kept_and_worldwide_requires_opt_in(self) -> None:
        key = feed_key("by", " AI Automation ")
        explicit = normalize_job(_job("by", ["Belarus", "Poland"]), key, "Belarus")
        self.assertEqual(explicit.location, "Remote - Belarus + other countries (Himalayas classification)")
        self.assertEqual(explicit.search_track, "international")
        self.assertEqual(explicit.description, "Build n8n & API workflows.")
        self.assertIsNone(normalize_job(_job("world", []), key, "Belarus"))
        worldwide = normalize_job(_job("world", []), key, "Belarus", include_worldwide=True)
        self.assertIn("Worldwide", worldwide.location)
        self.assertEqual(worldwide.search_track, "international")
        self.assertIsNone(normalize_job(_job("uk", ["United Kingdom"]), key, "Belarus"))

    @patch("app.integrations.job_sources.himalayas._get_page")
    def test_fetches_all_pages_and_deduplicates_before_import(self, get_page) -> None:
        get_page.side_effect = [
            {"limit": 2, "jobs": [_job("1", ["Belarus"]), _job("world", [])]},
            {"limit": 2, "jobs": [_job("2", [{"name": "Belarus"}])]},
        ]
        key, vacancies = fetch_search("BY", "automation", country_name="Belarus")
        self.assertEqual(key, "BY:automation:explicit")
        self.assertEqual([job.external_id.rsplit("/", 1)[-1] for job in vacancies], ["1", "2"])
        self.assertTrue(all(job.search_track == "belarus" for job in vacancies))
        self.assertEqual(get_page.call_count, 2)
        get_page.side_effect = [
            {"limit": 2, "jobs": [_job("1", ["Belarus"]), _job("1", ["Belarus"])]},
            {"limit": 2, "jobs": []},
        ]
        with self.assertRaisesRegex(HimalayasSourceError, "duplicate"):
            fetch_search("BY", "automation", country_name="Belarus")

    def test_rejects_malformed_or_external_links(self) -> None:
        key = feed_key("BY", "automation")
        with self.assertRaises(HimalayasSourceError):
            normalize_job(_job("1", ["Belarus"]) | {"applicationLink": "https://example.com/apply"},
                          key, "Belarus")
        with self.assertRaises(ValueError):
            feed_key("../BY", "automation")
        with self.assertRaises(ValueError):
            feed_key("BY", " ")

    @patch("app.integrations.job_sources.himalayas.urlopen")
    def test_search_request_uses_country_and_excludes_worldwide_by_default(self, urlopen) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, _limit):
                return json.dumps({"limit": 20, "jobs": [_job("1", ["Belarus"])]}).encode()

        urlopen.return_value = Response()
        fetch_search("BY", "AI Automation", country_name="Belarus")
        requested = urlopen.call_args.args[0].full_url
        self.assertIn("country=BY", requested)
        self.assertIn("exclude_worldwide=true", requested)
        self.assertIn("q=AI+Automation", requested)


if __name__ == "__main__":
    unittest.main()
