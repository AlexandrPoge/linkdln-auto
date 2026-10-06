"""Conservative, deterministic screening for digests and automatic approval.

Matching text is not proof of legal hiring eligibility. Ambiguous evidence stays
available for human review but never enters an automatic vacancy digest.
"""

import re
from dataclasses import dataclass

from app.candidate.profile import CandidateProfile
from app.matching.rules import evaluate, matched_profile_skills, title_rejection
from app.vacancies.models import Vacancy

_SENIOR = re.compile(r"\b(senior|sr\.?|staff|principal|lead|head|director|manager|старш\w*|ведущ\w*|руковод\w*)\b", re.I)
_BUSINESS = re.compile(
    r"\b(n8n|zapier|make\.com|workflows?|business processes|process automation|"
    r"API integrations?|CRM|RPA|LLMs?|large language models|webhooks?|"
    r"бизнес[ -]процесс\w*|автоматизац\w*\s+процесс\w*|интеграц\w*)\b", re.I)
_TEST_OR_INDUSTRIAL = re.compile(r"\b(selenium|cypress|playwright|PLC|SCADA|test automation|АСУ\w*|КИП\w*)\b", re.I)
_EU_ONLY = re.compile(
    r"\b(?:EU|EEA|European Union)\s+(?:only|residents?|citizens?|work (?:permit|authorization))\b|"
    r"\b(?:only|must|required to)\b[^.\n]{0,65}\b(?:based|reside|located|resident|live)\b"
    r"[^.\n]{0,30}\b(?:EU|EEA|European Union)\b|"
    r"\bonly (?:hire|hiring|recruit|recruiting)\b[^.\n]{0,30}\b(?:EU|EEA|European Union)\b|"
    r"\b(?:только|необходим\w*|обязательно)\b[^.\n]{0,70}\b(?:ЕС|Евросоюз\w*)\b", re.I)
_RESTRICTION = re.compile(
    r"\b(?:must (?:be (?:based|located)|reside|live)|only (?:hire|hiring|accept|consider)|"
    r"(?:candidates?|applicants?) (?:must|need to)|eligible countries|hiring (?:countries|locations)|"
    r"(?:role|position|job) is (?:only )?(?:open|available)|(?:remote|remotely) (?:only )?(?:within|from)|"
    r"(?:work|working) (?:permit|authorization)|right to work|"
    r"только (?:из|для|кандидат\w*)|необходим\w*\s+(?:прожива\w*|разрешен\w*))\b[^.\n]{0,180}", re.I)
_RESIDENCE = {"belarus": r"\b(?:Belarus|Беларус\w*|Белорус\w*)\b"}
_COUNTRIES = re.compile(
    r"\b(?:UK|United Kingdom|US|USA|United States|Canada|Poland|Portugal|Germany|France|"
    r"Spain|Italy|Netherlands|Ireland|Switzerland|Sweden|Norway|Finland|Denmark|"
    r"Belgium|Austria|Czechia|Czech Republic|Romania|Bulgaria|Greece|Hungary|"
    r"Lithuania|Latvia|Estonia|Croatia|Slovakia|Slovenia|Cyprus|Malta|Luxembourg|"
    r"Australia|India|Польш\w*|Германи\w*|США|Великобритани\w*|России)\b", re.I)
_GLOBAL_SCOPE = re.compile(r"\bremote\s*[-,]\s*(?:worldwide|anywhere|global)\b|\bwork from anywhere\b", re.I)
_EXPERIENCE = re.compile(r"\b\d+(?:\s*[-–]\s*\d+)?\+?\s*(?:years?\s+(?:of\s+)?(?:relevant\s+)?experience|"
                         r"лет\s+опыта|года?\s+опыта)\b", re.I)


@dataclass(frozen=True)
class ScreeningResult:
    status: str  # matched, manual, rejected
    reason: str


def screen_title(profile: CandidateProfile, title: str) -> str | None:
    if reason := title_rejection(profile, title):
        return reason
    if _SENIOR.search(title) and not any(_SENIOR.search(role) for role in profile.roles):
        return "Senior/lead role is outside the automatic search focus."
    return None


def screen(profile: CandidateProfile, job: Vacancy) -> ScreeningResult:
    if reason := screen_title(profile, job.title):
        return ScreeningResult("rejected", reason)
    match = evaluate(profile, job)
    if match.status == "rejected":
        return ScreeningResult("rejected", match.reasons[0])
    if len(job.description.strip()) < 80:
        return ScreeningResult("manual", "Full description is missing or too short to verify responsibilities.")
    if not _BUSINESS.search(job.description):
        if _TEST_OR_INDUSTRIAL.search(job.description):
            return ScreeningResult("rejected", "Description concerns test or industrial automation, not business workflows.")
        return ScreeningResult("manual", "Business/workflow automation responsibilities are not confirmed.")
    if len(matched_profile_skills(profile, job.description)) < 2:
        return ScreeningResult("manual", "Fewer than two profile skills are confirmed in the description.")
    if any(warning.startswith(("Office", "Remote work appears only")) for warning in match.warnings):
        return ScreeningResult("manual", "Fully remote work needs confirmation; office/hybrid or description-only evidence.")
    residence = profile.residence_country
    if not residence:
        return ScreeningResult("manual", "Country of residence is not configured.")
    if _EXPERIENCE.search(job.description):
        return ScreeningResult("manual", "Required years of experience are not verified against the candidate profile.")
    country = re.compile(_RESIDENCE.get(residence.casefold(), r"\b" + re.escape(residence) + r"\b"), re.I)
    if residence.casefold() == "belarus" and _EU_ONLY.search(job.description + "\n" + job.location):
        return ScreeningResult("rejected", "EU/EEA residence or citizenship is required; Belarus does not satisfy that scope.")
    for restriction in _RESTRICTION.finditer(job.description):
        text = restriction.group()
        if _COUNTRIES.search(text) and not country.search(text):
            return ScreeningResult("rejected", "Description restricts hiring to other countries.")
        # Even a country mention does not certify permits, nationality or tax status.
        return ScreeningResult("manual", "Residence/work-authorization restrictions need human confirmation.")
    confirmed_scope = _GLOBAL_SCOPE.search(job.location) or re.search(
        r"\bremote\s*[-,]\s*" + country.pattern, job.location, re.I)
    if job.source in {"hh", "himalayas"}:
        # hh areas and Himalayas' empty-restrictions => worldwide classification
        # are not employer hiring scopes. Require scope evidence in the description.
        confirmed_scope = _GLOBAL_SCOPE.search(job.description) or re.search(
            r"\bremote\s*[-,]\s*" + country.pattern, job.description, re.I)
    if not confirmed_scope:
        return ScreeningResult("manual", "Remote Europe/EMEA or a listing region does not confirm hiring from your residence.")
    if profile.salary_min is not None:
        return ScreeningResult("manual", "Minimum salary has not been verified.")
    return ScreeningResult("matched", "Role, workflow responsibilities, skills and remote residence scope match the published text.")
