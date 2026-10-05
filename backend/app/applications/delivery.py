"""A single, auditable delivery attempt for each approved application."""

from typing import Any, Protocol

from app.candidate.profile import is_email_address


class DeliverySender(Protocol):
    channel: str

    def can_send(self, review: dict[str, Any]) -> bool: ...
    def send(self, review: dict[str, Any]) -> str: ...


class DeliveryRepository(Protocol):
    def list_sendable_reviews(self, limit: int, *, track: str | None = None) -> list[dict[str, Any]]: ...
    def claim_delivery(self, review_id: int, channel: str,
                       expected_recipient: str | None = None) -> int | None: ...
    def finish_delivery(self, attempt_id: int, *, sent: bool, reference: str = "",
                        error: str = "") -> None: ...


def deliver_approved(repository: DeliveryRepository, sender: DeliverySender, *, limit: int = 10,
                     track: str | None = None, execute: bool = False) -> dict[str, int]:
    """Preview by default; ambiguous network outcomes are never retried automatically."""
    if not 1 <= limit <= 10:
        raise ValueError("limit must be between 1 and 10")
    rows = repository.list_sendable_reviews(limit, track=track)
    ready = [row for row in rows if sender.can_send(row)]
    result = {"eligible": len(rows), "ready": len(ready), "sent": 0, "uncertain": 0, "skipped": len(rows) - len(ready)}
    if not execute:
        return result
    for row in ready:
        attempt_id = repository.claim_delivery(row["id"], sender.channel, row.get("recipient_email"))
        if attempt_id is None:
            result["skipped"] += 1
            continue
        try:
            reference = sender.send(row)
        except Exception as exc:
            repository.finish_delivery(attempt_id, sent=False, error=str(exc)[:500])
            result["uncertain"] += 1
        else:
            repository.finish_delivery(attempt_id, sent=True, reference=reference[:200])
            result["sent"] += 1
    return result
