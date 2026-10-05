import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from app.__main__ import main
from app.candidate.profile import CandidateProfile
from app.integrations.job_sources.ashby import AshbySourceError
from app.vacancies.sync import parse_search_plan, run_searches


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict({
        "full_name": "Example", "roles": ["Automation Engineer"],
        "countries": ["Europe"], "skills": ["n8n"], "work_modes": ["remote"],
        "residence_country": "Belarus",
    })


class SyncTests(unittest.TestCase):
    def test_validates_sources_and_rejects_duplicates(self) -> None:
        plan = parse_search_plan({"sources": [
            {"source": "ashby", "board_token": "n8n", "company": "n8n"},
            {"source": "himalayas", "country": "BY", "query": "Automation Engineer"},
        ]})
        self.assertEqual([source.name for source in plan], ["ashby:n8n", "himalayas:BY:Automation Engineer"])
        for invalid in (
            {"sources": []},
            {"sources": [{"source": "linkedin", "board_token": "example"}]},
            {"sources": [{"source": ["ashby"], "board_token": "n8n"}]},
            {"sources": [{"source": "himalayas", "country": "PL", "query": "Engineer"}]},
            {"sources": [{"source": "ashby", "board_token": "../bad"}]},
            {"sources": [{"source": "greenhouse", "board_token": "ok", "query": "extra"}]},
            {"sources": [{"source": "ashby", "board_token": "n8n"},
                         {"source": "ashby", "board_token": "N8N"}]},
        ):
            with self.subTest(config=invalid), self.assertRaises(ValueError):
                parse_search_plan(invalid)

    @patch("app.vacancies.sync.refresh_queue")
    @patch("app.vacancies.sync.fetch_search")
    @patch("app.vacancies.sync.fetch_ashby_board")
    def test_imports_multiple_sources_and_refreshes_both_tracks(self, ashby, himalayas, refresh) -> None:
        repository = Mock()
        repository.get_profile.return_value = _profile()
        repository.import_board.side_effect = [{"total": 1, "new": 1, "closed": 0},
                                               {"total": 2, "new": 2, "closed": 0}]
        ashby.return_value = ["ashby-job"]
        himalayas.return_value = ("BY:automation engineer:explicit", ["himalayas-job"])
        refresh.return_value = {"matching": 1, "added": 1}
        plan = parse_search_plan({"sources": [
            {"source": "ashby", "board_token": "n8n", "company": "n8n"},
            {"source": "himalayas", "country": "BY", "query": "Automation Engineer"},
        ]})
        report = run_searches(repository, plan)
        self.assertEqual((report["successful"], report["failed"], report["sent"]), (2, 0, 0))
        repository.import_board.assert_any_call("n8n", ["ashby-job"], source="ashby")
        repository.import_board.assert_any_call("BY:automation engineer:explicit", ["himalayas-job"],
                                                source="himalayas", close_missing=False)
        self.assertEqual([call.kwargs["track"] for call in refresh.call_args_list], ["belarus", "international"])

    @patch("app.vacancies.sync.refresh_queue")
    @patch("app.vacancies.sync.fetch_ashby_board")
    @patch("app.vacancies.sync.fetch_greenhouse_board")
    def test_source_failure_does_not_close_missing_or_block_other_sources(self, greenhouse, ashby, refresh) -> None:
        repository = Mock()
        repository.get_profile.return_value = _profile()
        repository.import_board.return_value = {"total": 1, "new": 1, "closed": 0}
        greenhouse.return_value = ["greenhouse-job"]
        ashby.side_effect = AshbySourceError("temporary source failure")
        plan = parse_search_plan({"sources": [
            {"source": "ashby", "board_token": "n8n"},
            {"source": "greenhouse", "board_token": "example"},
        ]})
        report = run_searches(repository, plan)
        self.assertEqual((report["successful"], report["failed"]), (1, 1))
        repository.import_board.assert_called_once_with("example", ["greenhouse-job"], source="greenhouse")
        self.assertEqual(refresh.call_count, 2)

    @patch("app.vacancies.sync.refresh_queue")
    @patch("app.vacancies.sync.fetch_ashby_board", side_effect=AshbySourceError("failure"))
    def test_no_refresh_when_all_sources_fail(self, _ashby, refresh) -> None:
        repository = Mock()
        repository.get_profile.return_value = _profile()
        report = run_searches(repository, parse_search_plan({"sources": [
            {"source": "ashby", "board_token": "n8n"},
        ]}))
        self.assertIsNone(report["queue"])
        refresh.assert_not_called()
        repository.import_board.assert_not_called()

    @patch("app.__main__.run_searches")
    @patch("app.__main__.Repository")
    def test_sync_cli_reads_config_and_never_calls_delivery(self, repository_class, run) -> None:
        run.return_value = {"sources": [], "successful": 1, "failed": 0, "queue": {}, "sent": 0}
        output = io.StringIO()
        config = json.dumps({"sources": [{"source": "ashby", "board_token": "n8n"}]}).encode()
        with patch("pathlib.Path.read_bytes", return_value=config), \
             patch("app.__main__.sys.argv", ["app", "sync", "--config", "searches.json"]), \
             patch.dict("app.__main__.os.environ", {"DATABASE_URL": "postgresql://unused"}), \
             patch("app.__main__.deliver_approved") as deliver, redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(output.getvalue())["sent"], 0)
        repository_class.return_value.initialize.assert_called_once_with()
        deliver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
