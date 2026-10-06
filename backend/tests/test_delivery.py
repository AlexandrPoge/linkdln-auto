import unittest
from unittest.mock import Mock

from app.applications.delivery import deliver_approved


class DeliveryTests(unittest.TestCase):
    def test_preview_has_no_external_or_database_writes(self) -> None:
        repository = Mock()
        repository.list_sendable_reviews.return_value = [{"id": 7}, {"id": 8}]
        sender = Mock(channel="email")
        sender.can_send.side_effect = [True, False]
        self.assertEqual(deliver_approved(repository, sender),
                         {"eligible": 2, "ready": 1, "sent": 0, "uncertain": 0, "skipped": 1})
        repository.claim_delivery.assert_not_called()
        sender.send.assert_not_called()

    def test_claim_prevents_repeat_and_uncertain_result_is_not_retried(self) -> None:
        repository = Mock()
        repository.list_sendable_reviews.return_value = [{"id": 7}, {"id": 8}, {"id": 9}]
        repository.claim_delivery.side_effect = [17, None, 19]
        sender = Mock(channel="email")
        sender.can_send.return_value = True
        sender.send.side_effect = ["message-id", RuntimeError("timeout after send")]
        self.assertEqual(deliver_approved(repository, sender, execute=True),
                         {"eligible": 3, "ready": 3, "sent": 1, "uncertain": 1, "skipped": 1})
        repository.finish_delivery.assert_any_call(17, sent=True, reference="message-id")
        repository.finish_delivery.assert_any_call(19, sent=False, error="RuntimeError")
        self.assertEqual(sender.send.call_count, 2)

    def test_invalid_limit_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            deliver_approved(Mock(), Mock(), limit=0)
        with self.assertRaises(ValueError):
            deliver_approved(Mock(), Mock(), limit=11)
