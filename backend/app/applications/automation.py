"""Scheduled intake, deterministic applications, and a durable email digest."""

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.applications.delivery import deliver_approved, is_email_address
from app.integrations.email_sender import SMTPApplicationSender, SMTPSettings, send_email
from app.integrations.mailbox import collect_alerts
from app.matching.rules import evaluate
from app.templates.drafts import render_application
from app.vacancies.models import Vacancy
from app.vacancies.sync import run_searches


@dataclass(frozen=True, repr=False)
class AutomationSettings:
    email: str = ""
    password: str = ""
    resume_path: str = ""
    notifications: bool = True
    mailbox: bool = True
    send_applications: bool = True
    auto_approve: bool = True
    daily_limit: int = 5

    @classmethod
    def from_dict(cls, value: Any) -> "AutomationSettings":
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise ValueError("invalid automation settings")
        settings = cls(**value)
        if not isinstance(settings.email, str) or (settings.email and not is_email_address(settings.email)):
            raise ValueError("enter a valid email")
        if settings.email and not settings.email.casefold().endswith("@gmail.com"):
            raise ValueError("this connection currently supports personal Gmail accounts")
        if not isinstance(settings.password, str) or (settings.password and not re.fullmatch(r"[A-Za-z0-9]{16}", settings.password)):
            raise ValueError("enter the 16-character Gmail app password, not your account password")
        if not isinstance(settings.resume_path, str):
            raise ValueError("invalid resume path")
        if type(settings.daily_limit) is not int or not 1 <= settings.daily_limit <= 10:
            raise ValueError("application limit must be 1 to 10 per 24 hours")
        for name in ("notifications", "mailbox", "send_applications", "auto_approve"):
            if type(getattr(settings, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        return settings

    @property
    def connected(self) -> bool:
        return bool(self.email and self.password)

    def smtp(self, *, require_resume: bool = True) -> SMTPSettings:
        if not self.connected:
            raise ValueError("Gmail is not connected")
        filename, content = "", b""
        if require_resume:
            path = Path(self.resume_path)
            if not path.is_absolute() or not path.is_file() or not 1 <= path.stat().st_size <= 5_000_000:
                raise ValueError("select an existing absolute PDF path, at most 5 MB")
            content = path.read_bytes()
            if not content.startswith(b"%PDF-"):
                raise ValueError("resume is not a PDF")
            filename = path.name
        return SMTPSettings("smtp.gmail.com", 465, "ssl", self.email, self.password,
                            self.email, filename, content)


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> AutomationSettings:
        if not self.path.exists():
            return AutomationSettings()
        if self.path.stat().st_size > 16_000:
            raise ValueError("automation settings exceed 16 KB")
        return AutomationSettings.from_dict(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, settings: AutomationSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".automation-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({name: getattr(settings, name) for name in settings.__dataclass_fields__}, stream)
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


_APPLICATION_CONTACT = re.compile(
    r"(?:application email published by employer|apply(?:\s+(?:by|via|at|to|email))?|"
    r"send\s+(?:your\s+)?(?:cv|resume|application)|отправ\w*\s+(?:ваше\s+)?резюме|"
    r"присылайте\s+резюме)[^\n.@]{0,100}?([A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
    re.IGNORECASE,
)


def application_contact(description: str) -> str | None:
    addresses = {match.group(1).rstrip(".").lower() for match in _APPLICATION_CONTACT.finditer(description)}
    if len(addresses) != 1:
        return None
    email = next(iter(addresses))
    if email.split("@", 1)[0] in {"privacy", "legal", "support", "security", "abuse", "noreply", "no-reply"}:
        return None
    return email if is_email_address(email) else None


def automatic_candidate(profile, row: dict) -> str | None:
    if row["status"] != "draft" or not row["is_active"] or row.get("delivery_status"):
        return None
    # Aggregator/location-search classifications are not employer hiring scopes.
    if row["source"] not in {"greenhouse", "ashby"}:
        return None
    seen = row.get("last_seen_at")
    if not isinstance(seen, datetime) or seen < datetime.now(timezone.utc) - timedelta(hours=24):
        return None
    job = Vacancy(**{name: row[name] for name in Vacancy.__dataclass_fields__ if name in row})
    result = evaluate(profile, job)
    if result.status != "review" or result.score < 75 or len(result.matched_skills) < 2:
        return None
    if not re.search(r"\bremote\s*[-,].*\b(?:Belarus|Worldwide|Anywhere|Global)\b", job.location, re.I):
        return None
    if any(warning.startswith(("Senior", "Office", "Remote work appears only")) for warning in result.warnings):
        return None
    if row["draft_text"] != render_application(profile, job, result):
        return None  # Edited drafts belong to the user.
    return application_contact(job.description)


class AutomationPipeline:
    def __init__(self, repository, store: SettingsStore) -> None:
        self.repository = repository
        self.store = store

    def __call__(self, repository, sources) -> dict:
        with repository.automation_lock() as acquired:
            if not acquired:
                return {"sources": [], "successful": 0, "failed": 0, "queue": None,
                        "sent": 0, "automation": {"status": "another process is running"}}
            return self._run(repository, sources)

    def _run(self, repository, sources) -> dict:
        report = run_searches(repository, sources)
        settings = self.store.load()
        profile = repository.get_profile()
        stages: dict[str, Any] = {"connected": settings.connected, "mailbox": "not connected",
                                  "auto_approved": 0, "applications": "not connected", "digest": "not connected"}
        report["automation"] = stages
        # Durable pending notifications also accumulate before the account is connected.
        repository.enqueue_notifications()
        if not settings.connected:
            return report
        if settings.mailbox:
            try:
                stages["mailbox"] = collect_alerts(repository, settings.email, settings.password)
            except Exception:
                stages["mailbox"] = "failed: check Gmail access; other stages continue"
        else:
            stages["mailbox"] = "disabled"
        if settings.send_applications:
            try:
                smtp = settings.smtp()
                if settings.auto_approve:
                    for row in repository.list_reviews(500):
                        recipient = automatic_candidate(profile, row)
                        if recipient and recipient != settings.email.lower():
                            stages["auto_approved"] += repository.auto_approve_review(row, recipient)
                result = deliver_approved(repository, SMTPApplicationSender(smtp),
                                          limit=10, execute=True, daily_limit=settings.daily_limit)
                stages["applications"] = result
                report["sent"] = result["sent"]
            except Exception:
                stages["applications"] = "failed: check Gmail and resume PDF; no automatic retry"
        else:
            stages["applications"] = "disabled"
        repository.enqueue_notifications()
        if settings.notifications:
            try:
                items = repository.claim_notifications(25)
                if not items:
                    stages["digest"] = {"sent": 0, "items": 0}
                else:
                    body = "Новые вакансии и ссылки для Automation Engineer\n\n" + "\n\n".join(
                        f"{item['title']}\n{item['source']} · {item['kind']}\n{item['url']}" for item in items)
                    try:
                        reference = send_email(settings.smtp(require_resume=False), settings.email,
                                               f"Поиск работы: {len(items)} новых предложений", body)
                    except Exception:
                        repository.finish_notifications([item["id"] for item in items], sent=False)
                        stages["digest"] = "uncertain: check mailbox before any retry"
                    else:
                        repository.finish_notifications([item["id"] for item in items], sent=True,
                                                        reference=reference)
                        stages["digest"] = {"sent": 1, "items": len(items)}
            except Exception:
                stages["digest"] = "failed: check email connection"
        else:
            stages["digest"] = "disabled"
        return report

    def snapshot(self) -> dict:
        settings = self.store.load()
        return {"connected": settings.connected, "email": settings.email, "resume_path": settings.resume_path,
                "notifications": settings.notifications, "mailbox": settings.mailbox,
                "send_applications": settings.send_applications, "auto_approve": settings.auto_approve,
                "daily_limit": settings.daily_limit, **self.repository.automation_stats(),
                "leads": self.repository.list_alert_leads(20)}
