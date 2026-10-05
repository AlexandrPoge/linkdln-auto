import unittest

from app.vacancies.models import track_from_location


class SearchTrackTests(unittest.TestCase):
    def test_only_belarus_scoped_remote_jobs_use_belarus_track(self) -> None:
        self.assertEqual(track_from_location("Remote - Belarus"), "belarus")
        self.assertEqual(track_from_location("Remote - Belarus (Himalayas classification)"), "belarus")
        for location in (
            "Remote - Belarus, Poland", "Remote - Belarus; Remote - Europe",
            "Remote - Belarus, Remote - Europe", "Remote - Belarus + other countries",
            "Remote - Europe", "Hybrid - Belarus", "Remote",
        ):
            with self.subTest(location=location):
                self.assertEqual(track_from_location(location), "international")
