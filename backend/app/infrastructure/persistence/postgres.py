import json
from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.candidate.profile import CandidateProfile
from app.vacancies.models import Vacancy


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
                    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    UNIQUE (source, board_token, external_id)
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS vacancies_active_idx ON vacancies (is_active, last_seen_at DESC)")

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

    def import_board(self, board_token: str, vacancies: Sequence[Vacancy]) -> dict[str, int]:
        if any(job.source != "greenhouse" or job.board_token != board_token for job in vacancies):
            raise ValueError("all imported vacancies must belong to the requested Greenhouse board")
        ids = [job.external_id for job in vacancies]
        if len(ids) != len(set(ids)):
            raise ValueError("import contains duplicate job ids")
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT external_id FROM vacancies WHERE source = 'greenhouse' AND board_token = %s AND external_id = ANY(%s)",
                (board_token, ids),
            ).fetchall()
            existing_ids = {row["external_id"] for row in existing}
            for job in vacancies:
                connection.execute("""
                    INSERT INTO vacancies (source, board_token, external_id, company, title, location, description, url, source_updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source, board_token, external_id) DO UPDATE SET
                        company = EXCLUDED.company,
                        title = EXCLUDED.title,
                        location = EXCLUDED.location,
                        description = EXCLUDED.description,
                        url = EXCLUDED.url,
                        source_updated_at = EXCLUDED.source_updated_at,
                        last_seen_at = now(),
                        is_active = TRUE
                """, (job.source, job.board_token, job.external_id, job.company, job.title, job.location,
                      job.description, job.url, job.source_updated_at))
            closed = connection.execute("""
                UPDATE vacancies SET is_active = FALSE
                WHERE source = 'greenhouse' AND board_token = %s AND is_active = TRUE
                  AND NOT (external_id = ANY(%s))
            """, (board_token, ids)).rowcount
        return {"total": len(vacancies), "new": len(ids) - len(existing_ids), "closed": closed}

    def list_vacancies(self, limit: int = 20, *, active_only: bool = True) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connect() as connection:
            rows = connection.execute("""
                SELECT source, board_token, external_id, company, title, location, description, url,
                       source_updated_at, first_seen_at, last_seen_at, is_active
                FROM vacancies
                WHERE (NOT %s OR is_active)
                ORDER BY last_seen_at DESC, id DESC
                LIMIT %s
            """, (active_only, limit)).fetchall()
        return [dict(row) for row in rows]
