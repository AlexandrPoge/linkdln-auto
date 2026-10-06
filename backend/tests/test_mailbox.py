import unittest
from email.message import EmailMessage
from unittest.mock import Mock, patch

from app.integrations.mailbox import alert_links, collect_alerts


def message(sender="jobs-noreply@linkedin.com"):
    value = EmailMessage()
    value["From"] = sender
    value.set_content("Alert")
    value.add_alternative('<a href="https://www.linkedin.com/comm/jobs/view/12345678/?tracking=private">Engineer</a>'
                          '<a href="https://rabota.by/vacancy/123?from=email">Автоматизация</a>'
                          '<a href="https://hh.ru.evil.test/vacancy/999">Bad</a>', subtype="html")
    return value.as_bytes()


class MailboxTests(unittest.TestCase):
    def test_alert_links_are_canonical_and_never_trust_unknown_sender_domains(self):
        links = alert_links(message())
        self.assertEqual(len(links), 2)
        self.assertEqual(links[0]["url"], "https://www.linkedin.com/jobs/view/12345678/")
        self.assertEqual(links[1]["url"], "https://hh.ru/vacancy/123")
        self.assertEqual(alert_links(message("jobs@linkedin.com.evil.test")), [])

    @patch("app.integrations.mailbox.imaplib.IMAP4_SSL")
    def test_readonly_uid_cursor_and_peek_do_not_modify_mail(self, imap_class):
        mailbox = imap_class.return_value.__enter__.return_value
        mailbox.select.return_value = ("OK", [b"12"])
        mailbox.response.return_value = ("UIDVALIDITY", [b"42"])
        mailbox.uid.side_effect = [("OK", [b"9 10"]), ("OK", [b"1 (UID 10 RFC822.SIZE 1200)"]),
                                   ("OK", [(b"1 (BODY[])", message())])]
        repository = Mock()
        repository.mailbox_cursor.return_value = 9
        repository.save_alert_leads.return_value = 2
        result = collect_alerts(repository, "example@gmail.com", "a" * 16)
        self.assertEqual(result, {"processed": 1, "new_links": 2, "skipped": 0})
        mailbox.select.assert_called_once_with("INBOX", readonly=True)
        mailbox.uid.assert_any_call("fetch", "10", "(BODY.PEEK[])")
        self.assertEqual(repository.save_mailbox_cursor.call_args.args[-2:], ("42", 10))

    @patch("app.integrations.mailbox.imaplib.IMAP4_SSL")
    def test_failed_fetch_does_not_advance_cursor(self, imap_class):
        mailbox = imap_class.return_value.__enter__.return_value
        mailbox.select.return_value = ("OK", [b"12"])
        mailbox.response.return_value = ("UIDVALIDITY", [b"42"])
        mailbox.uid.side_effect = [("OK", [b"10"]), ("NO", [])]
        repository = Mock()
        repository.mailbox_cursor.return_value = 9
        with self.assertRaises(ValueError):
            collect_alerts(repository, "example@gmail.com", "a" * 16)
        repository.save_mailbox_cursor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
