import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from app.__main__ import main
from app.candidate.profile import CandidateProfile


class MatchCommandTests(unittest.TestCase):
    @patch("app.__main__.Repository")
    def test_matches_prints_ranked_unsent_drafts(self, repository_class) -> None:
        repository = repository_class.return_value
        repository.get_profile.return_value = CandidateProfile.from_dict({
            "full_name": "Example Candidate", "roles": ["Automation Developer"],
            "countries": ["Europe"], "skills": ["n8n"], "work_modes": ["remote"],
        })
        repository.list_vacancies.return_value = [
            {"source": "greenhouse", "board_token": "sample", "external_id": "1",
             "company": "Example", "title": "Automation Developer", "location": "Remote - Europe",
             "description": "Build n8n workflows.", "url": "https://example.com/1",
             "source_updated_at": None},
            {"source": "greenhouse", "board_token": "sample", "external_id": "2",
             "company": "Example", "title": "Sales Manager", "location": "Remote - Europe",
             "description": "Manage sales.", "url": "https://example.com/2",
             "source_updated_at": None},
        ]
        output = io.StringIO()
        with patch.dict("app.__main__.os.environ", {"DATABASE_URL": "postgresql://unused"}), \
             patch("app.__main__.sys.argv", ["app", "matches"]), redirect_stdout(output):
            self.assertEqual(main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["match"]["status"], "review")
        self.assertIn("n8n", result[0]["draft"])
        self.assertIn("n8n", result[0]["linkedin_message"])
        self.assertNotIn("linkedin_message", result[0]["draft"])
        repository.list_vacancies.assert_called_once_with(50)
        repository.initialize.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
