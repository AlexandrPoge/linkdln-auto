import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import psycopg

from app.candidate.profile import CandidateProfile
from app.api.review import serve
from app.applications.delivery import deliver_approved
from app.infrastructure.persistence.postgres import Repository
from app.integrations.email_sender import SMTPApplicationSender, SMTPSettings
from app.integrations.job_sources.ashby import AshbySourceError, fetch_board as fetch_ashby_board
from app.integrations.job_sources.greenhouse import SourceError, fetch_board
from app.integrations.job_sources.himalayas import HimalayasSourceError, fetch_search
from app.integrations.job_sources.linkedin_alert import parse_alert_email
from app.matching.rules import evaluate
from app.templates.drafts import render_application, render_linkedin_message
from app.vacancies.models import SEARCH_TRACKS, Vacancy
from app.vacancies.sync import parse_search_plan, run_searches


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
    importer.add_argument("source", choices=["greenhouse", "ashby", "himalayas"])
    importer.add_argument("board_token")
    importer.add_argument("--company", help="Display name for an Ashby board (defaults to its token)")
    importer.add_argument("--query", help="Search terms for Himalayas, such as 'AI Automation Specialist'")
    importer.add_argument("--include-worldwide", action="store_true",
                          help="Include Himalayas' worldwide-classified jobs; employer eligibility remains unverified")
    vacancies = commands.add_parser("vacancies", help="List saved vacancies")
    vacancies.add_argument("--limit", type=int, default=20)
    vacancies.add_argument("--all", action="store_true", help="Include closed vacancies")
    vacancies.add_argument("--track", choices=SEARCH_TRACKS, help="Filter Belarus or international search")
    matches = commands.add_parser("matches", help="Rank vacancies and prepare unsent drafts")
    matches.add_argument("--limit", type=int, default=50)
    matches.add_argument("--include-rejected", action="store_true")
    matches.add_argument("--track", choices=SEARCH_TRACKS, help="Filter Belarus or international search")
    server = commands.add_parser("serve", help="Open the local review queue (does not send applications)")
    server.add_argument("--port", type=int, default=8765)
    delivery = commands.add_parser("send-approved", help="Preview or email approved applications with explicit recipients")
    delivery.add_argument("--track", choices=SEARCH_TRACKS)
    delivery.add_argument("--limit", type=int, default=10)
    delivery.add_argument("--execute", action="store_true", help="Actually send emails; omitted by default")
    alert = commands.add_parser("inspect-linkedin-alert", help="Inspect a saved .eml; does not import or send")
    alert.add_argument("file", type=Path, help="Locally saved LinkedIn Job Alert .eml file")
    sync = commands.add_parser("sync", help="Fetch configured public sources and refresh unsent review queues")
    sync.add_argument("--config", type=Path, required=True, help="JSON search configuration")
    args = parser.parse_args()

    if args.command == "inspect-linkedin-alert":
        try:
            links = parse_alert_email(args.file.read_bytes())
            print(json.dumps({"job_links": [asdict(link) for link in links], "count": len(links),
                              "note": "Unverified email contents; no vacancy imported or message sent."},
                             ensure_ascii=False, indent=2))
        except (OSError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0

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
        elif args.command == "sync":
            config_bytes = args.config.read_bytes()
            if len(config_bytes) > 64_000:
                raise ValueError("search config exceeds 64 KB")
            plan = parse_search_plan(json.loads(config_bytes))
            result = run_searches(repository, plan)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1 if result["failed"] else 0
        elif args.command == "import":
            if args.source == "himalayas":
                saved = repository.get_profile()
                if saved is None or saved.residence_country != "Belarus" or args.board_token.upper() != "BY":
                    raise ValueError("Himalayas search currently requires the saved Belarus profile and country code BY")
                if not args.query or args.company:
                    raise ValueError("Himalayas import requires --query and does not support --company")
                key, jobs = fetch_search("BY", args.query, country_name="Belarus",
                                         include_worldwide=args.include_worldwide)
                result = repository.import_board(key, jobs, source="himalayas", close_missing=False)
            elif args.source == "ashby":
                if args.query or args.include_worldwide:
                    raise ValueError("--query and --include-worldwide are only supported for Himalayas")
                jobs = fetch_ashby_board(args.board_token, company=args.company)
                result = repository.import_board(args.board_token, jobs, source=args.source)
            else:
                if args.company or args.query or args.include_worldwide:
                    raise ValueError("Greenhouse import does not support these source-specific options")
                jobs = fetch_board(args.board_token)
                result = repository.import_board(args.board_token, jobs, source=args.source)
            print(json.dumps(result))
        elif args.command == "vacancies":
            print(json.dumps(repository.list_vacancies(args.limit, active_only=not args.all, track=args.track),
                             ensure_ascii=False, indent=2, default=_json_default))
        elif args.command == "serve":
            try:
                serve(repository, args.port)
            except KeyboardInterrupt:
                print("\nReview queue stopped")
        elif args.command == "send-approved":
            profile_for_email = repository.get_profile() if args.execute else None
            settings = (SMTPSettings.from_environment(default_from_email=profile_for_email.contact_email
                        if profile_for_email else None) if args.execute else None)
            result = deliver_approved(repository, SMTPApplicationSender(settings), limit=args.limit,
                                      track=args.track, execute=args.execute)
            print(json.dumps(result))
        else:
            saved = repository.get_profile()
            if saved is None:
                raise ValueError("candidate profile is missing; run 'profile set' first")
            results = []
            rows = (repository.list_vacancies(args.limit) if args.track is None
                    else repository.list_vacancies(args.limit, track=args.track))
            for row in rows:
                job = Vacancy(**{field: row[field] for field in Vacancy.__dataclass_fields__ if field in row})
                result = evaluate(saved, job)
                if result.status == "rejected" and not args.include_rejected:
                    continue
                results.append({
                    "vacancy": {"company": job.company, "title": job.title, "location": job.location,
                                "search_track": job.search_track,
                                "url": job.url, "source": job.source, "external_id": job.external_id},
                    "match": result.to_dict(),
                    "draft": render_application(saved, job, result) if result.status == "review" else None,
                    "linkedin_message": render_linkedin_message(saved, job, result)
                    if result.status == "review" else None,
                })
            results.sort(key=lambda item: item["match"]["score"], reverse=True)
            print(json.dumps(results, ensure_ascii=False, indent=2))
    except (OSError, ValueError, json.JSONDecodeError, SourceError, AshbySourceError,
            HimalayasSourceError, psycopg.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
