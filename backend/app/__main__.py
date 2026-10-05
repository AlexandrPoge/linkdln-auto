import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import psycopg

from app.candidate.profile import CandidateProfile
from app.infrastructure.persistence.postgres import Repository
from app.integrations.job_sources.greenhouse import SourceError, fetch_board


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Local job-search data commands")
    commands = parser.add_subparsers(dest="command", required=True)
    profile = commands.add_parser("profile", help="Store or show the candidate profile")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    set_profile = profile_commands.add_parser("set")
    set_profile.add_argument("file", type=Path, help="JSON file with candidate profile data")
    profile_commands.add_parser("show")
    importer = commands.add_parser("import", help="Import vacancies")
    importer.add_argument("source", choices=["greenhouse"])
    importer.add_argument("board_token")
    vacancies = commands.add_parser("vacancies", help="List saved vacancies")
    vacancies.add_argument("--limit", type=int, default=20)
    vacancies.add_argument("--all", action="store_true", help="Include closed vacancies")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL is required; see .env.example")
    repository = Repository(database_url)
    try:
        repository.initialize()
        if args.command == "profile" and args.profile_command == "set":
            value = json.loads(args.file.read_text(encoding="utf-8"))
            repository.save_profile(CandidateProfile.from_dict(value))
            print("Profile saved")
        elif args.command == "profile":
            saved = repository.get_profile()
            print(json.dumps(saved.to_dict() if saved else None, ensure_ascii=False, indent=2))
        elif args.command == "import":
            jobs = fetch_board(args.board_token)
            result = repository.import_board(args.board_token, jobs)
            print(json.dumps(result))
        else:
            print(json.dumps(repository.list_vacancies(args.limit, active_only=not args.all),
                             ensure_ascii=False, indent=2, default=_json_default))
    except (OSError, ValueError, json.JSONDecodeError, SourceError, psycopg.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
