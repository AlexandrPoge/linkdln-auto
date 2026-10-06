import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from app.applications.automation import (
    AutomationPipeline, AutomationSettings, SettingsStore, application_contact, automatic_candidate,
)
from app.candidate.profile import CandidateProfile
from app.matching.rules import evaluate
from app.templates.drafts import render_application
from app.vacancies.models import Vacancy


def profile():
    return CandidateProfile.from_dict({"full_name": "Example", "roles": ["Automation Engineer"],
        "countries": ["Europe"], "skills": ["n8n", "REST API"], "work_modes": ["remote"],
        "residence_country": "Belarus"})


def review():
    job = Vacancy("ashby", "example", "1", "Example", "Automation Engineer", "Remote - Worldwide",
                  "Build n8n and REST APIs.\nSend your resume to jobs@example.com", "https://example.com/1", None)
    return {name: getattr(job, name) for name in job.__dataclass_fields__} | {
        "id": 1, "status": "draft", "is_active": True, "delivery_status": None,
        "last_seen_at": datetime.now(timezone.utc),
        "draft_text": render_application(profile(), job, evaluate(profile(), job))}


class AutomationTests(unittest.TestCase):
    @patch("app.applications.automation.run_searches")
    @patch("app.applications.automation.collect_alerts")
    @patch("app.applications.automation.send_email")
    @patch("app.applications.automation.deliver_approved")
    def test_oauth_pipeline_uses_api_only_and_existing_delivery_guards(self, deliver, smtp_send, imap, search):
        search.return_value = {"sources": [], "successful": 1, "failed": 0, "sent": 0}
        deliver.return_value = {"sent": 1, "uncertain": 0}
        repository = Mock()
        repository.automation_lock.return_value = nullcontext(True)
        repository.list_reviews.return_value = []
        repository.claim_notifications.return_value = [{"id": 2, "title": "Engineer", "source": "ashby",
                                                        "kind": "Matched", "url": "https://example.com/2"}]
        store, oauth = Mock(), Mock()
        store.load.return_value = AutomationSettings(email="example@gmail.com")
        oauth.status.return_value = {"connected": True, "email": "example@gmail.com", "client_configured": True}
        oauth.collect_alerts.return_value = {"processed": 1, "new_links": 1}
        oauth.send.return_value = "gmail-digest-id"
        with patch.object(AutomationSettings, "attachment", return_value=("resume.pdf", b"%PDF-test")):
            report = AutomationPipeline(repository, store, oauth)(repository, ())
        self.assertEqual(report["sent"], 1)
        self.assertEqual(report["automation"]["transport"], "Gmail API")
        self.assertEqual(deliver.call_args.kwargs["daily_limit"], 5)
        self.assertEqual(deliver.call_args.args[1].channel, "email")
        oauth.collect_alerts.assert_called_once_with(repository)
        oauth.send.assert_called_once()
        smtp_send.assert_not_called()
        imap.assert_not_called()
        repository.finish_notifications.assert_called_once_with([2], sent=True, reference="gmail-digest-id")

    @patch("app.applications.automation.run_searches")
    def test_expired_oauth_access_does_not_consume_outbox(self, search):
        from app.integrations.gmail_api import GmailAPIError
        search.return_value = {"sources": [], "successful": 1, "failed": 0, "sent": 0}
        repository, store, oauth = Mock(), Mock(), Mock()
        repository.automation_lock.return_value = nullcontext(True)
        store.load.return_value = AutomationSettings(email="example@gmail.com", mailbox=False, send_applications=False)
        oauth.status.return_value = {"connected": True, "email": "example@gmail.com"}
        oauth.access_token.side_effect = GmailAPIError("Gmail API: подключи аккаунт заново")
        report = AutomationPipeline(repository, store, oauth)(repository, ())
        repository.claim_notifications.assert_not_called()
        self.assertIn("заново", report["automation"]["digest"])

    def test_settings_are_private_and_password_not_in_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "automation.json")
            settings = AutomationSettings.from_dict({"email": "example@gmail.com", "password": "a" * 16})
            store.save(settings)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.load(), settings)
            repository = Mock()
            repository.automation_stats.return_value = {"sent_total": 0}
            repository.list_alert_leads.return_value = []
            snapshot = AutomationPipeline(repository, store).snapshot()
            self.assertNotIn("password", snapshot)
            self.assertNotIn("a" * 16, repr(snapshot))

    def test_invalid_settings_are_rejected(self):
        for value in ({"email": "wrong"}, {"email": "a@example.com"}, {"daily_limit": True},
                      {"daily_limit": 11}, {"password": "account-password"}, {"mailbox": "yes"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                AutomationSettings.from_dict(value)

    def test_contact_requires_application_context_and_single_address(self):
        self.assertEqual(application_contact("Send your CV to jobs@example.com"), "jobs@example.com")
        self.assertEqual(application_contact("Присылайте резюме на jobs@example.com"), "jobs@example.com")
        self.assertIsNone(application_contact("Privacy: privacy@example.com"))
        self.assertIsNone(application_contact("Apply online. Privacy: privacy@example.com"))
        self.assertIsNone(application_contact("Apply to one@example.com\nApply to two@example.com"))

    def test_autoapproval_rejects_stale_edited_aggregator_or_restricted_jobs(self):
        row = review()
        self.assertEqual(automatic_candidate(profile(), row), "jobs@example.com")
        for changes in ({"status": "rejected"}, {"source": "remotive"}, {"location": "Remote - US"},
                        {"draft_text": "My edited draft"}, {"last_seen_at": datetime.now(timezone.utc) - timedelta(days=2)},
                        {"description": "Build n8n REST APIs. privacy@example.com"}):
            with self.subTest(changes=changes):
                self.assertIsNone(automatic_candidate(profile(), row | changes))

    @patch("app.applications.automation.run_searches")
    @patch("app.applications.automation.send_email")
    @patch("app.applications.automation.deliver_approved")
    def test_disconnected_pipeline_does_not_send(self, deliver, send, search):
        search.return_value = {"sources": [], "successful": 1, "failed": 0, "sent": 0}
        repository = Mock()
        repository.automation_lock.return_value = nullcontext(True)
        store = Mock()
        store.load.return_value = AutomationSettings()
        report = AutomationPipeline(repository, store)(repository, ())
        self.assertFalse(report["automation"]["connected"])
        repository.enqueue_notifications.assert_called_once()
        repository.claim_notifications.assert_not_called()
        deliver.assert_not_called()
        send.assert_not_called()

    @patch("app.applications.automation.run_searches")
    def test_second_process_does_not_run(self, search):
        repository = Mock()
        repository.automation_lock.return_value = nullcontext(False)
        report = AutomationPipeline(repository, Mock())(repository, ())
        self.assertEqual(report["sent"], 0)
        search.assert_not_called()

    @patch("app.applications.automation.run_searches")
    @patch("app.applications.automation.collect_alerts", side_effect=RuntimeError("private server message"))
    @patch("app.applications.automation.send_email", return_value="digest-id")
    @patch("app.applications.automation.deliver_approved")
    def test_mailbox_failure_does_not_block_sending_or_digest(self, deliver, send, collect, search):
        search.return_value = {"sources": [], "successful": 1, "failed": 0, "sent": 0}
        deliver.return_value = {"sent": 1, "uncertain": 0}
        repository = Mock()
        repository.automation_lock.return_value = nullcontext(True)
        repository.get_profile.return_value = profile()
        repository.list_reviews.return_value = [review()]
        repository.auto_approve_review.return_value = 1
        repository.claim_notifications.return_value = [{"id": 2, "title": "Engineer", "source": "ashby",
                                                        "kind": "Matched", "url": "https://example.com/2"}]
        store = Mock()
        store.load.return_value = AutomationSettings("example@gmail.com", "a" * 16)
        with patch.object(AutomationSettings, "smtp", return_value=Mock()):
            report = AutomationPipeline(repository, store)(repository, ())
        self.assertEqual(report["sent"], 1)
        self.assertEqual(report["automation"]["auto_approved"], 1)
        self.assertNotIn("private server message", str(report))
        self.assertEqual(deliver.call_args.kwargs["daily_limit"], 5)
        repository.finish_notifications.assert_called_once_with([2], sent=True, reference="digest-id")

    @patch("app.applications.automation.run_searches")
    @patch("app.applications.automation.send_email", side_effect=RuntimeError("timeout after SMTP DATA"))
    def test_uncertain_digest_is_finalized_without_retry(self, send, search):
        search.return_value = {"sources": [], "successful": 1, "failed": 0, "sent": 0}
        repository = Mock()
        repository.automation_lock.return_value = nullcontext(True)
        repository.claim_notifications.return_value = [{"id": 2, "title": "Engineer", "source": "hh",
                                                        "kind": "Lead", "url": "https://hh.ru/vacancy/2"}]
        store = Mock()
        store.load.return_value = AutomationSettings("example@gmail.com", "a" * 16, mailbox=False, send_applications=False)
        report = AutomationPipeline(repository, store)(repository, ())
        repository.finish_notifications.assert_called_once_with([2], sent=False)
        self.assertIn("uncertain", report["automation"]["digest"])
        self.assertEqual(send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
