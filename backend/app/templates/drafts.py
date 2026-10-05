from app.candidate.profile import CandidateProfile
from app.matching.rules import MatchResult
from app.vacancies.models import Vacancy


def _single_line(value: str) -> str:
    return " ".join(value.split())


def render_application(profile: CandidateProfile, vacancy: Vacancy, result: MatchResult) -> str:
    """Create an editable draft using only user-provided profile facts."""
    if result.status != "review":
        raise ValueError("cannot draft an application for a rejected vacancy")
    company = _single_line(vacancy.company)
    title = _single_line(vacancy.title)
    skills = ", ".join(result.matched_skills[:3])
    experience = f"My background includes {skills}. " if skills else ""
    return (
        f"Hello {company} hiring team,\n\n"
        f"I'm interested in the {title} role. {experience}"
        "I would welcome the opportunity to discuss how I could contribute.\n\n"
        f"Best regards,\n{profile.full_name}"
        + (f"\n{profile.contact_email}" if profile.contact_email else "")
    )


def render_linkedin_message(profile: CandidateProfile, vacancy: Vacancy, result: MatchResult) -> str:
    """Prepare a short, unsent recruiter message from verified profile skills."""
    if result.status != "review":
        raise ValueError("cannot draft a message for a rejected vacancy")
    company = _single_line(vacancy.company)
    title = _single_line(vacancy.title)
    skills = ", ".join(result.matched_skills[:3])
    background = f" I work with {skills}." if skills else ""
    return (
        f"Hello, I noticed the {title} opening at {company} and am interested in the role."
        f"{background} Would you be open to a brief conversation about it?\n\n"
        f"Best,\n{profile.full_name}"
    )
