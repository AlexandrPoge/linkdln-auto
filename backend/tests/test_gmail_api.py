import base64
import hashlib
import json
import tempfile
import time
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from app.integrations.gmail_api import (
    GmailOAuth, GmailAPIError, GmailApplicationSender, SCOPES, _request, _save,
)


CLIENT = json.dumps({"installed": {"client_id": "123-test.apps.googleusercontent.com", "client_secret": "private-client",
                                  "token_uri": "https://evil.test/steal"}})


def grant(**changes):
    return {"access_token": "private-access", "refresh_token": "private-refresh", "expires_in": 3600,
            "token_type": "Bearer", "scope": " ".join(SCOPES), **changes}


class GmailAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.oauth = GmailOAuth(Path(self.directory.name))

    def connect(self, **changes):
        self.oauth.configure(CLIENT)
        url = self.oauth.begin(8765, "international")
        state = parse_qs(urlsplit(url).query)["state"][0]
        with patch("app.integrations.gmail_api._request", side_effect=[grant(**changes), {"emailAddress": "example@gmail.com"}]):
            return self.oauth.finish(state, "private-code")

    def test_config_only_accepts_desktop_client_and_ignores_custom_endpoints(self):
        for raw in ("not json", json.dumps({"web": {}}), json.dumps({"installed": {"client_id": "evil", "client_secret": "x"}})):
            with self.assertRaises(GmailAPIError):
                self.oauth.configure(raw)
        self.oauth.configure(CLIENT)
        value = json.loads(self.oauth.client_path.read_text())
        self.assertEqual(set(value), {"client_id", "client_secret"})
        self.assertEqual(self.oauth.client_path.stat().st_mode & 0o777, 0o600)

    def test_pkce_state_expiry_and_replay(self):
        self.oauth.configure(CLIENT)
        url = self.oauth.begin(8765, "international")
        query = parse_qs(urlsplit(url).query)
        verifier = self.oauth._pending["verifier"]
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.assertEqual(query["code_challenge"], [expected])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["redirect_uri"], ["http://127.0.0.1:8765/gmail-callback"])
        self.assertNotIn(verifier, url)
        with patch("app.integrations.gmail_api._request") as request:
            with self.assertRaises(GmailAPIError):
                self.oauth.finish("wrong", "code")
            request.assert_not_called()
        self.oauth._pending["expires"] = time.time() - 1
        with self.assertRaises(GmailAPIError):
            self.oauth.finish(query["state"][0], "code")
        self.connect()
        with self.assertRaises(GmailAPIError):
            self.oauth.finish(query["state"][0], "code")

    def test_success_pauses_settings_before_private_tokens_are_saved(self):
        self.oauth.configure(CLIENT)
        url = self.oauth.begin(8765, "belarus")
        state = parse_qs(urlsplit(url).query)["state"][0]
        paused = []
        def pause(email):
            self.assertFalse(self.oauth.token_path.exists())
            paused.append(email)
        with patch("app.integrations.gmail_api._request", side_effect=[grant(), {"emailAddress": "example@gmail.com"}]) as request:
            self.assertEqual(self.oauth.finish(state, "code", on_connected=pause), ("belarus", "example@gmail.com"))
        self.assertEqual(paused, ["example@gmail.com"])
        self.assertEqual(request.call_args_list[0].args[0], "https://oauth2.googleapis.com/token")
        self.assertEqual(self.oauth.token_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("private-access", str(self.oauth.status()))
        self.assertNotIn("private-refresh", str(self.oauth.status()))

    def test_failed_settings_write_does_not_activate_account(self):
        self.oauth.configure(CLIENT)
        state = parse_qs(urlsplit(self.oauth.begin(8765, "belarus")).query)["state"][0]
        with patch("app.integrations.gmail_api._request", side_effect=[grant(), {"emailAddress": "example@gmail.com"}]):
            with self.assertRaises(OSError):
                self.oauth.finish(state, "code", on_connected=Mock(side_effect=OSError("disk full")))
        self.assertFalse(self.oauth.status()["connected"])

    def test_partial_permission_missing_refresh_and_denial_never_connect(self):
        for changes in ({"scope": SCOPES[0]}, {"refresh_token": ""}):
            with self.assertRaises(GmailAPIError):
                self.connect(**changes)
            self.assertFalse(self.oauth.status()["connected"])
        state = parse_qs(urlsplit(self.oauth.begin(8765, "belarus")).query)["state"][0]
        with patch("app.integrations.gmail_api._request") as request:
            with self.assertRaises(GmailAPIError):
                self.oauth.finish(state, "", error=True)
            request.assert_not_called()

    def test_refresh_retains_refresh_token_and_disconnect_is_local(self):
        self.connect()
        value = json.loads(self.oauth.token_path.read_text())
        _save(self.oauth.token_path, value | {"expires_at": 0})
        refreshed = grant(access_token="new-access")
        del refreshed["refresh_token"]
        with patch("app.integrations.gmail_api._request", return_value=refreshed) as request:
            self.assertEqual(self.oauth.access_token(), "new-access")
            self.assertEqual(self.oauth.access_token(), "new-access")
            self.assertEqual(request.call_count, 1)
        self.assertEqual(json.loads(self.oauth.token_path.read_text())["refresh_token"], "private-refresh")
        self.oauth.disconnect()
        self.assertFalse(self.oauth.status()["connected"])
        self.assertTrue(self.oauth.client_path.is_file())

    def test_send_pdf_is_mime_encoded_and_never_retried(self):
        self.connect()
        with patch.object(self.oauth, "api", return_value={"id": "sent-1"}) as api:
            sender = GmailApplicationSender(self.oauth, "resume.pdf", b"%PDF-test")
            reference = sender.send({"recipient_email": "jobs@example.com", "title": "Engineer", "company": "Example", "draft_text": "Hello"})
            self.assertEqual(reference, "sent-1")
            self.assertEqual(sender.channel, "email")
            raw = api.call_args.kwargs["payload"]["raw"]
            message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
            self.assertEqual(message["From"], "example@gmail.com")
            self.assertEqual(message["To"], "jobs@example.com")
            attachment = list(message.iter_attachments())[0]
            self.assertEqual(attachment.get_filename(), "resume.pdf")
            self.assertEqual(attachment.get_payload(decode=True), b"%PDF-test")
        with patch.object(self.oauth, "api", side_effect=GmailAPIError("network")) as api:
            with self.assertRaises(GmailAPIError):
                self.oauth.send("jobs@example.com", "Hi", "Body")
            self.assertEqual(api.call_count, 1)

    def test_https_errors_never_echo_server_body_or_credentials(self):
        for error in (HTTPError("https://oauth2.googleapis.com/token", 403, "private-access", {}, None), URLError("private-refresh")):
            if isinstance(error, HTTPError):
                self.addCleanup(error.close)
            with patch("app.integrations.gmail_api.build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(GmailAPIError) as result:
                    _request("https://oauth2.googleapis.com/token", form={"secret": "private-client"})
                for secret in ("private-client", "private-access", "private-refresh"):
                    self.assertNotIn(secret, str(result.exception))

    def test_api_rejects_untrusted_endpoint(self):
        with self.assertRaises(GmailAPIError):
            self.oauth.api("https://evil.test")

    def test_alert_reads_are_deduplicated_and_do_not_modify_labels(self):
        self.connect()
        raw = b"From: jobs-noreply@linkedin.com\nContent-Type: text/plain\n\nhttps://www.linkedin.com/jobs/view/12345678/"
        encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        repository = Mock()
        repository.save_alert_leads.return_value = 1
        with patch.object(self.oauth, "api", side_effect=[{"messages": [{"id": "abc"}]}, {"raw": encoded, "sizeEstimate": len(raw)}]) as api:
            self.assertEqual(self.oauth.collect_alerts(repository), {"processed": 1, "new_links": 1, "skipped": 0})
            self.assertEqual(api.call_args.kwargs["query"], {"format": "raw"})
        with patch.object(self.oauth, "api", return_value={"messages": [{"id": "abc"}]}) as api:
            self.assertEqual(self.oauth.collect_alerts(repository)["processed"], 0)
            self.assertEqual(api.call_count, 1)
        repository.save_alert_leads.assert_called_once()
        self.assertEqual(self.oauth.seen_path.stat().st_mode & 0o777, 0o600)

    def test_failed_alert_fetch_not_marked_processed(self):
        self.connect()
        with patch.object(self.oauth, "api", side_effect=[{"messages": [{"id": "abc"}]}, GmailAPIError("network")]):
            with self.assertRaises(GmailAPIError):
                self.oauth.collect_alerts(Mock())
        self.assertFalse(self.oauth.seen_path.exists())


if __name__ == "__main__":
    unittest.main()
