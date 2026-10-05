import threading
import unittest
from unittest.mock import Mock

from app.api.search_runner import SearchRunner
from app.vacancies.sync import SearchSource


class SearchRunnerTests(unittest.TestCase):
    def test_runs_immediately_and_prevents_overlapping_searches(self) -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def search(_repository, _sources):
            started.set()
            release.wait(timeout=5)
            finished.set()
            return {"sources": [], "successful": 1, "failed": 0}

        runner = SearchRunner(Mock(), (SearchSource("ashby", board_token="n8n"),),
                              interval_seconds=3600, search_fn=search)
        try:
            runner.start()
            self.assertTrue(started.wait(timeout=2))
            self.assertTrue(runner.snapshot()["running"])
            self.assertFalse(runner.trigger())
            release.set()
            self.assertTrue(finished.wait(timeout=2))
            for _ in range(100):
                if not runner.snapshot()["running"]:
                    break
                threading.Event().wait(0.01)
            snapshot = runner.snapshot()
            self.assertFalse(snapshot["running"])
            self.assertEqual(snapshot["report"]["successful"], 1)
            self.assertIsNotNone(snapshot["finished_at"])
        finally:
            release.set()
            runner.stop()

    def test_records_failure_without_crashing_server(self) -> None:
        done = threading.Event()

        def failure(_repository, _sources):
            done.set()
            raise RuntimeError("temporary failure")

        runner = SearchRunner(Mock(), (SearchSource("ashby", board_token="n8n"),),
                              search_fn=failure)
        self.assertTrue(runner.trigger())
        self.assertTrue(done.wait(timeout=2))
        for _ in range(100):
            if not runner.snapshot()["running"]:
                break
            threading.Event().wait(0.01)
        self.assertIn("temporary failure", runner.snapshot()["error"])
        runner.stop()


if __name__ == "__main__":
    unittest.main()
