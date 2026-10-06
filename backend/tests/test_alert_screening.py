import unittest
from unittest.mock import Mock, patch

from app.vacancies.alert_screening import check_alert_leads
from test_screening import profile, job


def lead(number=1, source="linkedin", title="Automation Engineer"):
    return {"id": number, "source": source, "external_id": str(number), "title": title,
            "url": "https://untrusted.example/not-used"}


class AlertScreeningTests(unittest.TestCase):
    @patch("app.vacancies.alert_screening.fetch_hh_detail")
    def test_linkedin_titles_are_classified_without_any_web_request(self, fetch):
        repository = Mock()
        repository.list_alert_leads_to_check.return_value = [lead(), lead(2, title="Backend Engineer"),
                                                           lead(3, title="Просмотреть")]
        counts = check_alert_leads(repository, profile())
        self.assertEqual(counts, {"checked": 3, "rejected": 1, "manual": 2, "resolved": 0, "detail_requests": 0})
        fetch.assert_not_called()
        repository.import_board.assert_not_called()

    @patch("app.vacancies.alert_screening.fetch_hh_detail")
    def test_hh_details_use_identity_not_untrusted_url_and_keep_other_boards_active(self, fetch):
        repository = Mock()
        repository.claim_source_refresh.return_value = True
        repository.list_alert_leads_to_check.return_value = [lead(source="hh", title="Перейти к вакансии")]
        fetch.return_value = job(source="hh", board_token="alerts")
        counts = check_alert_leads(repository, profile())
        self.assertEqual(counts["resolved"], 1)
        fetch.assert_called_once_with("1")
        repository.import_board.assert_called_once_with("alerts", [fetch.return_value], source="hh", close_missing=False)

    @patch("app.vacancies.alert_screening.fetch_hh_detail")
    def test_hh_error_stops_requests_and_preserves_manual_leads(self, fetch):
        from app.integrations.job_sources.public_search import PublicSearchError
        fetch.side_effect = PublicSearchError("api.hh.ru: HTTP 403")
        repository = Mock()
        repository.claim_source_refresh.return_value = True
        repository.list_alert_leads_to_check.return_value = [lead(n, "hh") for n in range(1, 11)]
        counts = check_alert_leads(repository, profile())
        self.assertEqual(counts["manual"], 10)
        self.assertEqual(counts["detail_requests"], 1)
        self.assertIn("403", counts["detail_error"])
        fetch.assert_called_once()

    @patch("app.vacancies.alert_screening.fetch_hh_detail")
    def test_hh_detail_requests_are_capped_at_eight(self, fetch):
        fetch.return_value = None
        repository = Mock()
        repository.claim_source_refresh.return_value = True
        repository.list_alert_leads_to_check.return_value = [lead(n, "hh") for n in range(1, 21)]
        counts = check_alert_leads(repository, profile())
        self.assertEqual(fetch.call_count, 8)
        self.assertEqual(counts["rejected"], 8)
        self.assertEqual(counts["manual"], 12)

    @patch("app.vacancies.alert_screening.fetch_hh_detail")
    def test_hh_cooldown_does_not_make_network_requests(self, fetch):
        repository = Mock()
        repository.claim_source_refresh.return_value = False
        repository.list_alert_leads_to_check.return_value = [lead(source="hh")]
        self.assertEqual(check_alert_leads(repository, profile())["manual"], 1)
        fetch.assert_not_called()
