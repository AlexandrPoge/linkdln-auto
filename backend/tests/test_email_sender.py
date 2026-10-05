import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.integrations.email_sender import SMTPApplicationSender, SMTPSettings


class EmailSenderTests(unittest.TestCase):
    def test_preview_never_sends_and_requires_explicit_recipient(self) -> None:
        sender = SMTPApplicationSender()
        self.assertFalse(sender.can_send({"recipient_email": None}))
        self.assertFalse(sender.can_send({"recipient_email": "bad-address"}))
        self.assertTrue(sender.can_send({"recipient_email": "jobs@example.com"}))
        with self.assertRaisesRegex(ValueError, "preview mode"):
            sender.send({"recipient_email": "jobs@example.com"})

    def test_settings_require_valid_sender_and_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            resume = Path(directory) / "resume.pdf"
            resume.write_bytes(b"%PDF-1.4\nexample")
            settings = {
                "SMTP_HOST": "smtp.example.com", "SMTP_PORT": "465", "SMTP_SECURITY": "ssl",
                "SMTP_USERNAME": "user", "SMTP_PASSWORD": "secret", "SMTP_FROM": "me@example.com",
                "RESUME_PDF_PATH": str(resume),
            }
            with patch.dict(os.environ, settings, clear=True):
                self.assertEqual(SMTPSettings.from_environment().resume_bytes, resume.read_bytes())
            with patch.dict(os.environ, settings | {"SMTP_FROM": "invalid"}, clear=True):
                with self.assertRaisesRegex(ValueError, "SMTP_FROM"):
                    SMTPSettings.from_environment()
            with patch.dict(os.environ, {key: value for key, value in settings.items() if key != "SMTP_FROM"},
                            clear=True):
                self.assertEqual(SMTPSettings.from_environment(
                    default_from_email="candidate@example.com").from_email, "candidate@example.com")

    @patch("app.integrations.email_sender.smtplib.SMTP_SSL")
    def test_ssl_send_attaches_resume_and_returns_message_id(self, smtp_class) -> None:
        settings = SMTPSettings("smtp.example.com", 465, "ssl", "user", "secret", "me@example.com",
                                "resume.pdf", b"%PDF-1.4\nexample")
        smtp = smtp_class.return_value.__enter__.return_value
        smtp.send_message.return_value = {}
        review = {"recipient_email": "jobs@example.com", "title": "Automation Developer",
                  "company": "Example", "draft_text": "Hello, I would like to apply."}
        result = SMTPApplicationSender(settings).send(review)
        self.assertTrue(result.startswith("<"))
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "jobs@example.com")
        self.assertEqual(message["From"], "me@example.com")
        self.assertIn("Automation Developer", message["Subject"])
        self.assertEqual(len(message.iter_attachments().__next__().get_payload(decode=True)),
                         len(settings.resume_bytes))
        smtp.login.assert_called_once_with("user", "secret")
