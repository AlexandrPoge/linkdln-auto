from dataclasses import dataclass
from typing import Any
import re


def is_email_address(value: str) -> bool:
    return bool(len(value) <= 254 and re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", value
    ))


def _text(value: Any, field: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    result = value.strip()
    if required and not result:
        raise ValueError(f"{field} cannot be empty")
    return result


def _strings(value: Any, field: str, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    result = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
    if required and not result:
        raise ValueError(f"{field} cannot be empty")
    return result


@dataclass(frozen=True)
class CandidateProfile:
    full_name: str
    resume_text: str
    roles: tuple[str, ...]
    countries: tuple[str, ...]
    skills: tuple[str, ...]
    work_modes: tuple[str, ...]
    salary_min: int | None
    salary_currency: str | None
    residence_country: str | None
    contact_email: str | None = None

    @classmethod
    def from_dict(cls, value: Any) -> "CandidateProfile":
        if not isinstance(value, dict):
            raise ValueError("profile must be a JSON object")
        salary_min = value.get("salary_min")
        if salary_min is not None and (isinstance(salary_min, bool) or not isinstance(salary_min, int) or salary_min <= 0):
            raise ValueError("salary_min must be a positive integer or null")
        salary_currency = value.get("salary_currency")
        if salary_currency is not None:
            salary_currency = _text(salary_currency, "salary_currency").upper()
            if len(salary_currency) != 3 or not salary_currency.isalpha():
                raise ValueError("salary_currency must be a three-letter code")
        if (salary_min is None) != (salary_currency is None):
            raise ValueError("salary_min and salary_currency must be set together")
        work_modes = _strings(value.get("work_modes", []), "work_modes")
        if any(mode not in {"remote", "hybrid", "onsite"} for mode in work_modes):
            raise ValueError("work_modes must contain only remote, hybrid, or onsite")
        residence_country = value.get("residence_country")
        if residence_country is not None:
            residence_country = _text(residence_country, "residence_country", required=True)
        contact_email = value.get("contact_email")
        if contact_email is not None:
            contact_email = _text(contact_email, "contact_email", required=True)
            if not is_email_address(contact_email):
                raise ValueError("contact_email must be a valid email address")
        return cls(
            full_name=_text(value.get("full_name"), "full_name", required=True),
            resume_text=_text(value.get("resume_text", ""), "resume_text"),
            roles=_strings(value.get("roles"), "roles", required=True),
            countries=_strings(value.get("countries"), "countries", required=True),
            skills=_strings(value.get("skills", []), "skills"),
            work_modes=work_modes,
            salary_min=salary_min,
            salary_currency=salary_currency,
            residence_country=residence_country,
            contact_email=contact_email,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "resume_text": self.resume_text,
            "roles": list(self.roles),
            "countries": list(self.countries),
            "skills": list(self.skills),
            "work_modes": list(self.work_modes),
            "salary_min": self.salary_min,
            "salary_currency": self.salary_currency,
            "residence_country": self.residence_country,
            "contact_email": self.contact_email,
        }
