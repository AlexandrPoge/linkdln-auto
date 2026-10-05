import re
from dataclasses import dataclass

from app.candidate.profile import CandidateProfile
from app.vacancies.models import Vacancy

_GENERIC_ROLE_WORDS = {"ai", "engineer", "developer", "software", "specialist", "разработчик", "инженер"}
_ROLE_TYPES = {
    "engineer": re.compile(r"\b(engineer|engineering|инженер\w*)\b", re.IGNORECASE),
    "developer": re.compile(r"\b(developer|разработчик\w*)\b", re.IGNORECASE),
    "specialist": re.compile(r"\b(specialist|специалист\w*)\b", re.IGNORECASE),
}
_REMOTE = re.compile(r"\b(remote|fully distributed|work from anywhere|home[ -]?office|удал[её]н\w*)\b", re.IGNORECASE)
_NEGATED_REMOTE = re.compile(r"\b(no|not)\s+remote\b", re.IGNORECASE)
_OFFICE = re.compile(r"\b(on[ -]?site|office|hybrid|гибрид\w*|офис\w*)\b", re.IGNORECASE)
_SENIOR_TITLE = re.compile(r"\b(senior|sr\.?|staff|principal|lead|head|director|manager)\b", re.IGNORECASE)
_MANAGEMENT_TITLE = re.compile(r"\b(manager|head|director)\b", re.IGNORECASE)
_QA_TITLE = re.compile(r"\b(QA|quality assurance|test automation|automation test)\b", re.IGNORECASE)
_NON_EU_SCOPE = re.compile(r"\b(us|usa|united states|canada|australia|india|apac|americas|north america)\b", re.IGNORECASE)
_BROAD_SCOPE = re.compile(r"\b(europe|emea|worldwide|anywhere|global)\b", re.IGNORECASE)
_RESIDENCE_ALIASES = {"united kingdom": ("UK",), "united states": ("US", "USA")}


def _words(value: str) -> set[str]:
    return set(re.findall(r"\w+", value.casefold()))


def _role_types(value: str) -> set[str]:
    return {name for name, pattern in _ROLE_TYPES.items() if pattern.search(value)}


def _contains_phrase(text: str, phrase: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(phrase.casefold()) + r"(?!\w)", text.casefold()))


def _skill_mentioned(text: str, skill: str) -> bool:
    name = skill.casefold()
    if name in {"go", "golang"}:
        return bool(re.search(r"(?<!\w)(Go|Golang)(?!\w)", text))
    if name in {"rest api", "rest apis"}:
        return bool(re.search(r"(?<!\w)REST APIs?(?!\w)", text, re.IGNORECASE))
    if name in {"webhook", "webhooks"}:
        return bool(re.search(r"(?<!\w)webhooks?(?!\w)", text, re.IGNORECASE))
    if name in {"llm", "large language models"}:
        return bool(re.search(r"(?<!\w)(LLMs?|large language models?)(?!\w)", text, re.IGNORECASE))
    return _contains_phrase(text, skill)


def _skill_key(skill: str) -> str:
    name = skill.casefold()
    for group in ({"go", "golang"}, {"rest api", "rest apis"},
                  {"webhook", "webhooks"}, {"llm", "large language models"}):
        if name in group:
            return sorted(group)[0]
    return name


def _remote_scopes(location: str) -> tuple[str, ...]:
    parts = re.split(r";|,\s*(?=remote\b)", location, flags=re.IGNORECASE)
    scopes: list[str] = []
    for part in parts:
        match = re.search(r"\bremote\s*[-,]\s*(.+)$", part.strip(), re.IGNORECASE)
        if match:
            scopes.append(match.group(1).strip())
    return tuple(scopes)


def _residence_explicitly_excluded(description: str, residence: str) -> bool:
    country = re.escape(residence)
    before = re.compile(
        rf"\b(?:except|excluding|excluded|not hiring (?:in|from)|cannot hire (?:in|from)|"
        rf"unable to consider applicants|restricted countries|do not work with|don't work with)\b"
        rf"[^.\n]{{0,250}}\b{country}\b", re.IGNORECASE,
    )
    after = re.compile(rf"\b{country}\b[^.\n]{{0,80}}\b(?:excluded|ineligible|not eligible)\b", re.IGNORECASE)
    return bool(before.search(description) or after.search(description))


@dataclass(frozen=True)
class MatchResult:
    status: str
    score: int
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    matched_skills: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "score": self.score,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "matched_skills": list(self.matched_skills),
        }


def evaluate(profile: CandidateProfile, vacancy: Vacancy) -> MatchResult:
    """Rank a vacancy for human review; never decide that an application may be sent."""
    title = vacancy.title.casefold()
    if _MANAGEMENT_TITLE.search(title) and not any(_MANAGEMENT_TITLE.search(role) for role in profile.roles):
        return MatchResult("rejected", 0, ("Management role is outside the target roles.",), (), ())
    if _QA_TITLE.search(title) and not any(_QA_TITLE.search(role) for role in profile.roles):
        return MatchResult("rejected", 0, ("QA/test automation is outside the target roles.",), (), ())
    exact_role = next((role for role in profile.roles if _contains_phrase(title, role)), None)
    role_terms = set().union(*(_words(role) - _GENERIC_ROLE_WORDS for role in profile.roles))
    shared_terms = role_terms & _words(title)
    if not exact_role and not shared_terms:
        return MatchResult("rejected", 0, ("Job title does not match target roles.",), (), ())
    target_types = set().union(*(_role_types(role) for role in profile.roles))
    if not exact_role and target_types and not target_types.intersection(_role_types(title)):
        return MatchResult("rejected", 0, ("Job title does not match the target role type.",), (), ())

    remote_only = profile.work_modes == ("remote",)
    location = vacancy.location.casefold()
    description = vacancy.description.casefold()
    location_without_home_office = re.sub(r"\bhome[ -]?office\b", "", location)
    remote_in_location = bool(_REMOTE.search(location))
    office_in_location = bool(_OFFICE.search(location_without_home_office))
    if remote_only and (_NEGATED_REMOTE.search(location) or _NEGATED_REMOTE.search(description)):
        return MatchResult("rejected", 0, ("Vacancy explicitly excludes remote work.",), (), ())
    if remote_only and office_in_location and not remote_in_location:
        return MatchResult("rejected", 0, ("Location is listed as office-based or hybrid.",), (), ())
    if remote_only and not (remote_in_location or _REMOTE.search(description)):
        return MatchResult("rejected", 0, ("Remote work is not confirmed.",), (), ())
    if profile.residence_country and _residence_explicitly_excluded(vacancy.description, profile.residence_country):
        return MatchResult("rejected", 0,
                           (f"The vacancy explicitly excludes applicants from {profile.residence_country}.",), (), ())
    scopes = _remote_scopes(vacancy.location)
    if remote_only and profile.residence_country and scopes:
        residence_names = (profile.residence_country,) + _RESIDENCE_ALIASES.get(profile.residence_country.casefold(), ())
        if not any(_BROAD_SCOPE.search(scope) or any(_contains_phrase(scope, name) for name in residence_names)
                   for scope in scopes):
            return MatchResult("rejected", 0,
                               (f"Listed remote locations do not include {profile.residence_country} or a broad region.",),
                               (), ())
    if any(country.casefold() == "europe" for country in profile.countries) and not any(
        _contains_phrase(location, country) for country in profile.countries
    ):
        scopes = [part.strip() for part in vacancy.location.split(";") if part.strip()]
        if scopes and all(_NON_EU_SCOPE.search(scope) for scope in scopes):
            return MatchResult("rejected", 0, ("Listed remote locations are outside the target European region.",), (), ())

    score = 50 if exact_role else 30
    reasons = [f"Target role matched: {exact_role}." if exact_role else
               f"Target role keywords matched: {', '.join(sorted(shared_terms))}."]
    warnings: list[str] = []
    if remote_in_location:
        score += 15
        reasons.append("Remote work appears in the listed location.")
    elif remote_only:
        warnings.append("Remote work appears only in the description; verify the actual work mode.")
    if remote_only and (office_in_location or _OFFICE.search(description)):
        warnings.append("Office or hybrid terms also appear; confirm the role is fully remote.")

    matched_skills_list: list[str] = []
    seen_skills: set[str] = set()
    for skill in profile.skills:
        key = _skill_key(skill)
        if key not in seen_skills and _skill_mentioned(vacancy.title + " " + vacancy.description, skill):
            matched_skills_list.append(skill)
            seen_skills.add(key)
    matched_skills = tuple(matched_skills_list)
    if matched_skills:
        score += min(30, len(matched_skills) * 10)
        reasons.append("Profile skills appear in the vacancy: " + ", ".join(matched_skills) + ".")
    else:
        warnings.append("No listed profile skills were found in the vacancy; check technical requirements.")
    if _SENIOR_TITLE.search(vacancy.title):
        warnings.append("Senior or leadership title: verify the required experience before approval.")

    target_locations = [country for country in profile.countries if _contains_phrase(location, country)]
    if vacancy.search_track == "belarus" and profile.residence_country and _contains_phrase(
        location, profile.residence_country
    ):
        score += 10
        reasons.append(f"Listed location matches country of residence: {profile.residence_country}.")
    elif target_locations:
        score += 10
        reasons.append("Listed location matches a target region: " + ", ".join(target_locations) + ".")
    else:
        warnings.append("Listed location does not confirm a target region; review manually.")
    residence = f" ({profile.residence_country})" if profile.residence_country else ""
    warnings.append(f"Confirm that the employer accepts applications from your country of residence{residence}.")
    if vacancy.source == "himalayas":
        country = profile.residence_country or "your country"
        warnings.append(f"Himalayas is an aggregator: confirm this posting and eligibility from {country} with the employer.")
    if profile.salary_min is not None:
        warnings.append("Salary requirement has not been verified against this vacancy.")

    return MatchResult("review", min(score, 100), tuple(reasons), tuple(warnings), matched_skills)
