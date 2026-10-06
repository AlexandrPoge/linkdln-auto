import json
from collections.abc import Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.applications.queue import QueueCandidate
from app.applications.delivery import is_email_address
from app.candidate.profile import CandidateProfile
from app.vacancies.models import SEARCH_TRACKS, Vacancy

# Eligibility expires when profile or vacancy evidence changes. No mailbox label
# can satisfy this join. Reuse it for both outbox claims and dashboard counters.
_CURRENT_SCREENING = """
    JOIN vacancy_screenings AS s ON s.vacancy_id = v.id
    JOIN candidate_profiles AS p ON p.id = 1 AND p.data = s.profile_data
    WHERE s.status = 'matched' AND v.is_active
      AND v.last_seen_at > now() - interval '24 hours'
      AND s.evidence <@ to_jsonb(v)
"""
_ELIGIBLE_NOTIFICATION = f"""EXISTS (
    SELECT 1 FROM vacancies AS v {_CURRENT_SCREENING}
      AND v.source = n.source AND v.external_id = n.external_id
      AND NOT EXISTS (SELECT 1 FROM application_reviews AS r
                      WHERE r.vacancy_id = v.id AND r.status = 'rejected')
)"""


class Repository:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS candidate_profiles (
                    id SMALLINT PRIMARY KEY CHECK (id = 1),
                    data JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS vacancies (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    source TEXT NOT NULL,
                    board_token TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    company TEXT NOT NULL,
                    title TEXT NOT NULL,
                    location TEXT NOT NULL,
                    description TEXT NOT NULL,
                    url TEXT NOT NULL,
                    source_updated_at TIMESTAMPTZ,
                    search_track TEXT NOT NULL DEFAULT 'international' CHECK (search_track IN ('belarus', 'international')),
                    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    UNIQUE (source, board_token, external_id)
                )
            """)
            connection.execute("""
                ALTER TABLE vacancies ADD COLUMN IF NOT EXISTS search_track TEXT NOT NULL
                    DEFAULT 'international' CHECK (search_track IN ('belarus', 'international'))
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS vacancies_active_idx ON vacancies (is_active, last_seen_at DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS vacancies_track_idx ON vacancies (search_track, is_active, last_seen_at DESC)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS application_reviews (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    vacancy_id BIGINT NOT NULL UNIQUE REFERENCES vacancies(id),
                    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'rejected')),
                    score SMALLINT NOT NULL CHECK (score BETWEEN 0 AND 100),
                    reasons JSONB NOT NULL,
                    warnings JSONB NOT NULL,
                    draft_text TEXT NOT NULL,
                    recipient_email TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    approved_at TIMESTAMPTZ,
                    CHECK ((status = 'approved') = (approved_at IS NOT NULL))
                )
            """)
            connection.execute("ALTER TABLE application_reviews ADD COLUMN IF NOT EXISTS recipient_email TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS application_reviews_status_idx ON application_reviews (status, updated_at DESC)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS application_delivery_attempts (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    review_id BIGINT NOT NULL UNIQUE REFERENCES application_reviews(id),
                    vacancy_source TEXT NOT NULL,
                    vacancy_external_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'sending' CHECK (status IN ('sending', 'sent', 'uncertain')),
                    external_reference TEXT,
                    error TEXT,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    finished_at TIMESTAMPTZ
                )
            """)
            connection.execute("ALTER TABLE application_delivery_attempts ADD COLUMN IF NOT EXISTS vacancy_source TEXT")
            connection.execute("ALTER TABLE application_delivery_attempts ADD COLUMN IF NOT EXISTS vacancy_external_id TEXT")
            connection.execute("""
                UPDATE application_delivery_attempts AS d
                SET vacancy_source = v.source, vacancy_external_id = v.external_id
                FROM application_reviews AS r JOIN vacancies AS v ON v.id = r.vacancy_id
                WHERE d.review_id = r.id AND (d.vacancy_source IS NULL OR d.vacancy_external_id IS NULL)
            """)
            connection.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS application_delivery_source_job_idx
                ON application_delivery_attempts (vacancy_source, vacancy_external_id)
                WHERE vacancy_source IS NOT NULL AND vacancy_external_id IS NOT NULL
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS source_refreshes (
                    source TEXT PRIMARY KEY, finished_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS mailbox_cursors (
                    account TEXT PRIMARY KEY, epoch TEXT NOT NULL, last_uid BIGINT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS alert_leads (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    source TEXT NOT NULL, external_id TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
                    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(source, external_id)
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS job_notifications (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    source TEXT NOT NULL, external_id TEXT NOT NULL, title TEXT NOT NULL,
                    url TEXT NOT NULL, kind TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sending','sent','uncertain')),
                    external_reference TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE(source, external_id)
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS vacancy_screenings (
                    vacancy_id BIGINT PRIMARY KEY REFERENCES vacancies(id),
                    status TEXT NOT NULL CHECK(status IN ('matched','manual','rejected')),
                    reason TEXT NOT NULL, evidence JSONB NOT NULL, profile_data JSONB NOT NULL,
                    checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            connection.execute("""
                ALTER TABLE alert_leads ADD COLUMN IF NOT EXISTS screening_status TEXT NOT NULL
                    DEFAULT 'pending' CHECK(screening_status IN ('pending','manual','rejected','resolved'))
            """)
            connection.execute("ALTER TABLE alert_leads ADD COLUMN IF NOT EXISTS screening_reason TEXT NOT NULL DEFAULT ''")
            connection.execute("ALTER TABLE alert_leads ADD COLUMN IF NOT EXISTS checked_at TIMESTAMPTZ")

    def save_profile(self, profile: CandidateProfile) -> None:
        with self._connect() as connection:
            current = connection.execute("SELECT data FROM candidate_profiles WHERE id = 1").fetchone()
            if current and current["data"] != profile.to_dict():
                connection.execute("""UPDATE alert_leads SET screening_status = 'pending',
                    screening_reason = '', checked_at = NULL WHERE screening_status != 'resolved'""")
            connection.execute("""
                INSERT INTO candidate_profiles (id, data) VALUES (1, %s::jsonb)
                ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()
            """, (json.dumps(profile.to_dict()),))

    def get_profile(self) -> CandidateProfile | None:
        with self._connect() as connection:
            row = connection.execute("SELECT data FROM candidate_profiles WHERE id = 1").fetchone()
        return CandidateProfile.from_dict(row["data"]) if row else None

    def import_board(self, board_token: str, vacancies: Sequence[Vacancy], *, source: str = "greenhouse",
                     close_missing: bool = True) -> dict[str, int]:
        if source not in {"greenhouse", "ashby", "himalayas", "hh", "remotive"}:
            raise ValueError("unsupported vacancy source")
        if any(job.source != source or job.board_token != board_token for job in vacancies):
            raise ValueError("all imported vacancies must belong to the requested source and board")
        ids = [job.external_id for job in vacancies]
        if len(ids) != len(set(ids)):
            raise ValueError("import contains duplicate job ids")
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT external_id FROM vacancies WHERE source = %s AND board_token = %s AND external_id = ANY(%s)",
                (source, board_token, ids),
            ).fetchall()
            existing_ids = {row["external_id"] for row in existing}
            for job in vacancies:
                connection.execute("""
                    INSERT INTO vacancies (source, board_token, external_id, company, title, location, description, url, source_updated_at, search_track)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source, board_token, external_id) DO UPDATE SET
                        company = EXCLUDED.company,
                        title = EXCLUDED.title,
                        location = EXCLUDED.location,
                        description = EXCLUDED.description,
                        url = EXCLUDED.url,
                        source_updated_at = EXCLUDED.source_updated_at,
                        search_track = EXCLUDED.search_track,
                        last_seen_at = now(),
                        is_active = TRUE
                """, (job.source, job.board_token, job.external_id, job.company, job.title, job.location,
                      job.description, job.url, job.source_updated_at, job.search_track))
            closed = 0
            if close_missing:
                closed = connection.execute("""
                    UPDATE vacancies SET is_active = FALSE
                    WHERE source = %s AND board_token = %s AND is_active = TRUE
                      AND NOT (external_id = ANY(%s))
                """, (source, board_token, ids)).rowcount
        return {"total": len(vacancies), "new": len(ids) - len(existing_ids), "closed": closed}

    def list_vacancies(self, limit: int = 20, *, active_only: bool = True,
                       track: str | None = None) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if track is not None and track not in SEARCH_TRACKS:
            raise ValueError("invalid search track")
        with self._connect() as connection:
            rows = connection.execute("""
                SELECT id, source, board_token, external_id, company, title, location, description, url,
                       source_updated_at, search_track, first_seen_at, last_seen_at, is_active
                FROM vacancies
                WHERE (NOT %s OR is_active) AND (%s::text IS NULL OR search_track = %s)
                ORDER BY last_seen_at DESC, id DESC
                LIMIT %s
            """, (active_only, track, track, limit)).fetchall()
        return [dict(row) for row in rows]

    def enqueue_reviews(self, candidates: list[QueueCandidate]) -> int:
        if len({candidate.vacancy_id for candidate in candidates}) != len(candidates):
            raise ValueError("queue contains duplicate vacancies")
        with self._connect() as connection:
            added = 0
            for candidate in candidates:
                added += connection.execute("""
                    INSERT INTO application_reviews (vacancy_id, score, reasons, warnings, draft_text)
                    SELECT id, %s, %s::jsonb, %s::jsonb, %s
                    FROM vacancies WHERE id = %s AND is_active = TRUE
                    ON CONFLICT (vacancy_id) DO NOTHING
                """, (candidate.score, json.dumps(candidate.reasons), json.dumps(candidate.warnings),
                      candidate.draft_text, candidate.vacancy_id)).rowcount
        return added

    def list_reviews(self, limit: int = 100, *, track: str | None = None) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if track is not None and track not in SEARCH_TRACKS:
            raise ValueError("invalid search track")
        with self._connect() as connection:
            rows = connection.execute("""
                SELECT r.id, r.status, r.score, r.reasons, r.warnings, r.draft_text, r.recipient_email,
                       r.created_at, r.updated_at, r.approved_at,
                       d.status AS delivery_status, d.channel AS delivery_channel,
                       v.source, v.board_token, v.external_id, v.source_updated_at, v.last_seen_at,
                       v.company, v.title, v.location, v.description, v.url, v.is_active, v.search_track
                FROM application_reviews AS r
                JOIN vacancies AS v ON v.id = r.vacancy_id
                LEFT JOIN application_delivery_attempts AS d ON d.review_id = r.id
                WHERE (%s::text IS NULL OR v.search_track = %s)
                ORDER BY CASE r.status WHEN 'draft' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
                         r.score DESC, r.updated_at DESC
                LIMIT %s
            """, (track, track, limit)).fetchall()
        return [dict(row) for row in rows]

    def list_sendable_reviews(self, limit: int = 10, *, track: str | None = None) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if track is not None and track not in SEARCH_TRACKS:
            raise ValueError("invalid search track")
        with self._connect() as connection:
            rows = connection.execute("""
                SELECT r.id, r.draft_text, r.recipient_email, v.source, v.board_token, v.external_id,
                       v.company, v.title, v.url, v.search_track
                FROM application_reviews AS r
                JOIN vacancies AS v ON v.id = r.vacancy_id
                WHERE r.status = 'approved' AND v.is_active = TRUE
                  AND v.last_seen_at > now() - interval '24 hours'
                  AND (%s::text IS NULL OR v.search_track = %s)
                  AND r.recipient_email IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM application_delivery_attempts AS d
                                  WHERE d.review_id = r.id OR (d.vacancy_source = v.source AND d.vacancy_external_id = v.external_id))
                ORDER BY r.approved_at, r.id
                LIMIT %s
            """, (track, track, limit)).fetchall()
        return [dict(row) for row in rows]

    def claim_delivery(self, review_id: int, channel: str, expected_recipient: str | None = None,
                       *, daily_limit: int | None = None) -> int | None:
        if review_id <= 0 or not channel or len(channel) > 50:
            raise ValueError("invalid delivery claim")
        with self._connect() as connection:
            if daily_limit is not None:
                if type(daily_limit) is not int or not 1 <= daily_limit <= 10:
                    raise ValueError("daily limit must be 1 to 10")
                connection.execute("SELECT pg_advisory_xact_lock(8765002)")
                count = connection.execute("SELECT count(*) AS n FROM application_delivery_attempts WHERE started_at > now() - interval '24 hours'").fetchone()["n"]
                if count >= daily_limit:
                    return None
            row = connection.execute("""
                INSERT INTO application_delivery_attempts
                    (review_id, vacancy_source, vacancy_external_id, channel)
                SELECT r.id, v.source, v.external_id, %s FROM application_reviews AS r
                JOIN vacancies AS v ON v.id = r.vacancy_id
                WHERE r.id = %s AND r.status = 'approved' AND v.is_active = TRUE
                  AND (%s::text IS NULL OR r.recipient_email = %s)
                  AND NOT EXISTS (
                      SELECT 1 FROM application_delivery_attempts AS prior
                      JOIN application_reviews AS prior_review ON prior_review.id = prior.review_id
                      JOIN vacancies AS prior_vacancy ON prior_vacancy.id = prior_review.vacancy_id
                      WHERE prior_vacancy.source = v.source AND prior_vacancy.external_id = v.external_id
                  )
                ON CONFLICT DO NOTHING RETURNING id
            """, (channel, review_id, expected_recipient, expected_recipient)).fetchone()
        return row["id"] if row else None

    def set_review_recipient(self, review_id: int, email: str) -> bool:
        address = email.strip()
        if review_id <= 0 or not is_email_address(address):
            raise ValueError("enter a valid employer email address")
        with self._connect() as connection:
            changed = connection.execute("""
                UPDATE application_reviews AS r SET recipient_email = %s, updated_at = now()
                FROM vacancies AS v
                WHERE r.id = %s AND r.vacancy_id = v.id AND v.is_active = TRUE
                  AND r.status = 'approved'
                  AND NOT EXISTS (SELECT 1 FROM application_delivery_attempts AS d WHERE d.review_id = r.id)
            """, (address, review_id)).rowcount
        return changed == 1

    @contextmanager
    def automation_lock(self):
        with self._connect() as connection:
            acquired = connection.execute("SELECT pg_try_advisory_lock(8765001) AS acquired").fetchone()["acquired"]
            try:
                yield acquired
            finally:
                if acquired:
                    connection.execute("SELECT pg_advisory_unlock(8765001)")

    def claim_source_refresh(self, source: str, seconds: int) -> bool:
        with self._connect() as connection:
            row = connection.execute("""
                INSERT INTO source_refreshes(source) VALUES (%s)
                ON CONFLICT(source) DO UPDATE SET finished_at = now()
                WHERE source_refreshes.finished_at < now() - %s * interval '1 second'
                RETURNING source
            """, (source, seconds)).fetchone()
        return row is not None

    def mailbox_cursor(self, account: str, epoch: str) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT last_uid FROM mailbox_cursors WHERE account = %s AND epoch = %s", (account, epoch)).fetchone()
        return row["last_uid"] if row else 0

    def save_mailbox_cursor(self, account: str, epoch: str, uid: int) -> None:
        with self._connect() as connection:
            connection.execute("""
                INSERT INTO mailbox_cursors(account, epoch, last_uid) VALUES (%s, %s, %s)
                ON CONFLICT(account) DO UPDATE SET epoch = EXCLUDED.epoch, last_uid = EXCLUDED.last_uid
            """, (account, epoch, uid))

    def save_alert_leads(self, links: list[dict]) -> int:
        added = 0
        with self._connect() as connection:
            for link in links:
                added += connection.execute("""
                    INSERT INTO alert_leads(source, external_id, title, url) VALUES (%s, %s, %s, %s)
                    ON CONFLICT(source, external_id) DO NOTHING
                """, (link["source"], link["external_id"], link["title"], link["url"])).rowcount
        return added

    def list_alert_leads(self, limit: int = 20) -> list[dict]:
        if not 1 <= limit <= 100:
            raise ValueError("lead limit must be 1 to 100")
        with self._connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM alert_leads ORDER BY first_seen_at DESC, id DESC LIMIT %s", (limit,)).fetchall()]

    def alert_screening_stats(self) -> dict:
        with self._connect() as connection:
            return {row["screening_status"]: row["n"] for row in connection.execute(
                "SELECT screening_status, count(*) AS n FROM alert_leads GROUP BY screening_status").fetchall()}

    def vacancy_screening_stats(self) -> dict:
        with self._connect() as connection:
            return {row["status"]: row["n"] for row in connection.execute("""
                SELECT s.status, count(*) AS n FROM vacancies AS v
                JOIN vacancy_screenings AS s ON s.vacancy_id = v.id
                JOIN candidate_profiles AS p ON p.id = 1 AND p.data = s.profile_data
                WHERE v.is_active AND s.evidence <@ to_jsonb(v)
                GROUP BY s.status
            """).fetchall()}

    def list_alert_leads_to_check(self, limit: int = 500) -> list[dict]:
        if not 1 <= limit <= 500:
            raise ValueError("lead check limit must be 1 to 500")
        with self._connect() as connection:
            return [dict(row) for row in connection.execute("""
                SELECT * FROM alert_leads WHERE screening_status = 'pending'
                    OR (source = 'hh' AND screening_status = 'manual' AND checked_at < now() - interval '6 hours')
                ORDER BY id LIMIT %s
            """, (limit,)).fetchall()]

    def finish_alert_screening(self, lead_id: int, status: str, reason: str) -> None:
        if status not in {"manual", "rejected", "resolved"} or not reason or len(reason) > 1000:
            raise ValueError("invalid alert screening result")
        with self._connect() as connection:
            connection.execute("UPDATE alert_leads SET screening_status = %s, screening_reason = %s, checked_at = now() WHERE id = %s",
                               (status, reason, lead_id))

    def save_screening(self, vacancy_id, result, evidence: dict, profile: CandidateProfile) -> bool:
        with self._connect() as connection:
            return connection.execute("""
                INSERT INTO vacancy_screenings(vacancy_id, status, reason, evidence, profile_data)
                SELECT v.id, %s, %s, %s::jsonb, %s::jsonb FROM vacancies AS v
                JOIN candidate_profiles AS p ON p.id = 1
                WHERE v.id = %s AND v.is_active AND %s::jsonb <@ to_jsonb(v) AND p.data = %s::jsonb
                ON CONFLICT(vacancy_id) DO UPDATE SET status = EXCLUDED.status, reason = EXCLUDED.reason,
                    evidence = EXCLUDED.evidence, profile_data = EXCLUDED.profile_data, checked_at = now()
            """, (result.status, result.reason, json.dumps(evidence), json.dumps(profile.to_dict()), vacancy_id,
                  json.dumps(evidence), json.dumps(profile.to_dict()))).rowcount == 1

    def auto_approve_review(self, row: dict, recipient: str) -> int:
        if not is_email_address(recipient):
            raise ValueError("invalid published application email")
        with self._connect() as connection:
            return connection.execute(f"""
                UPDATE application_reviews AS r SET status = 'approved', approved_at = now(),
                    recipient_email = %s, updated_at = now()
                FROM vacancies AS v {_CURRENT_SCREENING}
                    AND r.id = %s AND r.vacancy_id = v.id
                    AND r.status = 'draft' AND r.draft_text = %s AND v.description = %s AND v.location = %s AND v.title = %s
            """, (recipient, row["id"], row["draft_text"], row["description"], row["location"], row["title"])).rowcount

    def enqueue_notifications(self) -> None:
        with self._connect() as connection:
            connection.execute(f"""
                INSERT INTO job_notifications(source, external_id, title, url, kind)
                SELECT DISTINCT ON (v.source, v.external_id)
                    v.source, v.external_id, v.title || ' — ' || v.company, v.url, 'Прошла проверку описания'
                FROM vacancies AS v {_CURRENT_SCREENING}
                  AND NOT EXISTS (SELECT 1 FROM application_reviews AS r WHERE r.vacancy_id = v.id AND r.status = 'rejected')
                ORDER BY v.source, v.external_id, v.last_seen_at DESC, v.id DESC
                ON CONFLICT(source, external_id) DO UPDATE SET
                    title = EXCLUDED.title, url = EXCLUDED.url, kind = EXCLUDED.kind
                WHERE job_notifications.status = 'pending'
            """)

    def claim_notifications(self, limit: int = 25) -> list[dict]:
        if not 1 <= limit <= 25:
            raise ValueError("notification limit must be 1 to 25")
        with self._connect() as connection:
            rows = connection.execute(f"""
                WITH pending AS (SELECT n.id FROM job_notifications AS n WHERE n.status = 'pending'
                    AND {_ELIGIBLE_NOTIFICATION}
                    ORDER BY n.id LIMIT %s FOR UPDATE OF n SKIP LOCKED)
                UPDATE job_notifications AS n SET status = 'sending' FROM pending
                WHERE n.id = pending.id RETURNING n.*
            """, (limit,)).fetchall()
        return [dict(row) for row in rows]

    def finish_notifications(self, ids: list[int], *, sent: bool, reference: str = "") -> None:
        with self._connect() as connection:
            connection.execute("UPDATE job_notifications SET status = %s, external_reference = %s WHERE id = ANY(%s) AND status = 'sending'", ("sent" if sent else "uncertain", reference[:200] or None, ids))

    def automation_stats(self) -> dict:
        with self._connect() as connection:
            sent = connection.execute("SELECT count(*) AS n FROM application_delivery_attempts WHERE status = 'sent'").fetchone()["n"]
            pending = connection.execute(f"SELECT count(*) AS n FROM job_notifications AS n WHERE n.status = 'pending' AND {_ELIGIBLE_NOTIFICATION}").fetchone()["n"]
            held = connection.execute(f"SELECT count(*) AS n FROM job_notifications AS n WHERE n.status = 'pending' AND NOT ({_ELIGIBLE_NOTIFICATION})").fetchone()["n"]
            uncertain = connection.execute("SELECT count(*) AS n FROM application_delivery_attempts WHERE status IN ('uncertain','sending')").fetchone()["n"]
            uncertain += connection.execute("SELECT count(*) AS n FROM job_notifications WHERE status IN ('uncertain','sending')").fetchone()["n"]
        return {"sent_total": sent, "notification_pending": pending, "notification_held": held, "uncertain_total": uncertain}

    def finish_delivery(self, attempt_id: int, *, sent: bool, reference: str = "", error: str = "") -> None:
        if attempt_id <= 0:
            raise ValueError("invalid delivery attempt")
        with self._connect() as connection:
            changed = connection.execute("""
                UPDATE application_delivery_attempts
                SET status = %s, external_reference = %s, error = %s, finished_at = now()
                WHERE id = %s AND status = 'sending'
            """, ("sent" if sent else "uncertain", reference or None, error or None, attempt_id)).rowcount
        if changed != 1:
            raise ValueError("delivery attempt is already finalized")

    def edit_review(self, review_id: int, draft_text: str) -> bool:
        if review_id <= 0 or not draft_text.strip() or len(draft_text) > 10_000:
            raise ValueError("draft must contain 1 to 10,000 characters")
        with self._connect() as connection:
            changed = connection.execute("""
                UPDATE application_reviews AS r SET draft_text = %s, updated_at = now()
                FROM vacancies AS v
                WHERE r.id = %s AND r.vacancy_id = v.id AND v.is_active = TRUE AND r.status = 'draft'
            """, (draft_text.strip(), review_id)).rowcount
        return changed == 1

    def approve_reviews(self, review_ids: list[int]) -> int:
        if not review_ids or len(review_ids) > 100 or len(set(review_ids)) != len(review_ids) or any(i <= 0 for i in review_ids):
            raise ValueError("select 1 to 100 distinct draft ids")
        with self._connect() as connection:
            rows = connection.execute("""
                SELECT r.id, r.status, v.is_active
                FROM application_reviews AS r
                JOIN vacancies AS v ON v.id = r.vacancy_id
                WHERE r.id = ANY(%s)
                FOR UPDATE OF r, v
            """, (review_ids,)).fetchall()
            if len(rows) != len(review_ids) or any(row["status"] != "draft" or not row["is_active"] for row in rows):
                raise ValueError("every selected item must be an active draft")
            changed = connection.execute("""
                UPDATE application_reviews
                SET status = 'approved', approved_at = now(), updated_at = now()
                WHERE id = ANY(%s)
            """, (review_ids,)).rowcount
        return changed

    def reject_review(self, review_id: int) -> bool:
        if review_id <= 0:
            raise ValueError("review id must be positive")
        with self._connect() as connection:
            changed = connection.execute("""
                UPDATE application_reviews SET status = 'rejected', updated_at = now()
                WHERE id = %s AND status = 'draft'
            """, (review_id,)).rowcount
        return changed == 1

    def restore_review(self, review_id: int) -> bool:
        if review_id <= 0:
            raise ValueError("review id must be positive")
        with self._connect() as connection:
            changed = connection.execute("""
                UPDATE application_reviews AS r SET status = 'draft', updated_at = now()
                FROM vacancies AS v
                WHERE r.id = %s AND r.vacancy_id = v.id AND v.is_active = TRUE AND r.status = 'rejected'
            """, (review_id,)).rowcount
        return changed == 1
