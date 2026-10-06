"""Classify alert labels and resolve bounded hh details, never crawl LinkedIn."""

import re

from app.integrations.job_sources.public_search import PublicSearchError, fetch_hh_detail
from app.matching.screening import screen_title

_UNKNOWN_LABELS = {"просмотреть", "view", "view job", "apply", "по этой ссылке →",
                   "linkedin: ссылка из уведомления", "hh: ссылка из уведомления"}
_TITLE_WORDS = re.compile(r"\b(engineer|developer|architect|scientist|manager|specialist|senior|"
                          r"automation|инженер\w*|разработчик\w*|специалист\w*|автоматизац\w*)\b", re.I)


def check_alert_leads(repository, profile) -> dict:
    counts = {"checked": 0, "rejected": 0, "manual": 0, "resolved": 0, "detail_requests": 0}
    hh_available = None
    for lead in repository.list_alert_leads_to_check(500):
        title = lead["title"].strip()
        reason = (screen_title(profile, title) if title.casefold() not in _UNKNOWN_LABELS
                  and _TITLE_WORDS.search(title) else None)
        if reason:
            status = "rejected"
            reason = "Unverified alert title: " + reason
        elif lead["source"] == "hh":
            if hh_available is None:
                hh_available = repository.claim_source_refresh("hh-alert-details", 21600)
            if hh_available and counts["detail_requests"] < 8:
                counts["detail_requests"] += 1
                try:
                    # Build a fixed API endpoint from the numeric identity, not email URLs.
                    job = fetch_hh_detail(lead["external_id"])
                except PublicSearchError as exc:
                    status, reason = "manual", str(exc) + "; description unavailable"
                    counts["detail_error"] = str(exc)
                    hh_available = False  # Do not hammer a blocked/unavailable source.
                else:
                    if job:
                        repository.import_board(job.board_token, [job], source="hh", close_missing=False)
                        status, reason = "resolved", "Full description loaded from the official hh API."
                    else:
                        status, reason = "rejected", "hh vacancy is archived or not fully remote."
            else:
                status, reason = "manual", "hh description unavailable; bounded API limit/cooldown, retry later."
        else:
            status, reason = "manual", "Only an alert link is available; full description and hiring scope are not verified."
        repository.finish_alert_screening(lead["id"], status, reason)
        counts[status] += 1
        counts["checked"] += 1
    return counts
