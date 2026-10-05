import json
from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.applications.queue import QueueCandidate
from app.applications.delivery import is_email_address
from app.candidate.profile import CandidateProfile
from app.vacancies.models import SEARCH_TRACKS, Vacancy


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

    def save_profile(self, profile: CandidateProfile) -> None:
        with self._connect() as connection:
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
        if source not in {"greenhouse", "ashby", "himalayas"}:
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
                       v.source, v.company, v.title, v.location, v.description, v.url, v.is_active, v.search_track
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
                  AND (%s::text IS NULL OR v.search_track = %s)
                  AND NOT EXISTS (SELECT 1 FROM application_delivery_attempts AS d WHERE d.review_id = r.id)
                ORDER BY r.approved_at, r.id
                LIMIT %s
            """, (track, track, limit)).fetchall()
        return [dict(row) for row in rows]

    def claim_delivery(self, review_id: int, channel: str, expected_recipient: str | None = None) -> int | None:
        if review_id <= 0 or not channel or len(channel) > 50:
            raise ValueError("invalid delivery claim")
        with self._connect() as connection:
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
