import io
import json
import unittest
from contextlib import redirect_stdout
from email.message import EmailMessage
from unittest.mock import patch

from app.__main__ import main
from app.integrations.job_sources.linkedin_alert import parse_alert_email


def _email() -> bytes:
    message = EmailMessage()
    message["From"] = "Job Alerts <alerts@linkedin.com>"
    message["Subject"] = "Automation Engineer jobs"
    message.set_content("Job: https://www.linkedin.com/jobs/view/1234567890/?tracking=one")
    message.add_alternative("""
        <a href="https://www.linkedin.com/comm/jobs/view/1234567890/?trk=mail">
          Automation Engineer at Example
        </a>
        <a href="https://evil.example/jobs/view/9876543210/">Other job</a>
        <a href="https://www.linkedin.com/in/person-1234567890/">Profile</a>
    """, subtype="html")
    return message.as_bytes()


class LinkedInAlertTests(unittest.TestCase):
    def test_extracts_only_direct_job_links_and_deduplicates(self) -> None:
        jobs = parse_alert_email(_email())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, "1234567890")
        self.assertEqual(jobs[0].label, "Automation Engineer at Example")
        self.assertEqual(jobs[0].url, "https://www.linkedin.com/jobs/view/1234567890/")

    def test_sender_is_not_treated_as_authentication(self) -> None:
        self.assertEqual(parse_alert_email(_email().replace(b"alerts@linkedin.com", b"fake@example.com"))[0].job_id,
                         "1234567890")
        self.assertEqual(parse_alert_email(b"From: LinkedIn <alerts@linkedin.com>\n\nNo job links"), [])

    def test_rejects_oversize_email(self) -> None:
        with self.assertRaisesRegex(ValueError, "2 MB"):
            parse_alert_email(b"x" * 2_000_001)

    def test_cli_inspects_without_database_or_network(self) -> None:
        output = io.StringIO()
        with patch("pathlib.Path.read_bytes", return_value=_email()), \
             patch("app.__main__.sys.argv", ["app", "inspect-linkedin-alert", "alert.eml"]), \
             patch.dict("app.__main__.os.environ", {}, clear=True), redirect_stdout(output):
            self.assertEqual(main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["count"], 1)
        self.assertIn("no vacancy imported", result["note"])


if __name__ == "__main__":
    unittest.main()
