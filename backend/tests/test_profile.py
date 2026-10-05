import unittest

from app.candidate.profile import CandidateProfile


class CandidateProfileTests(unittest.TestCase):
    def test_normalizes_profile_and_preserves_facts(self) -> None:
        profile = CandidateProfile.from_dict({
            "full_name": "  Ada Lovelace  ",
            "resume_text": "  Built analytical systems.  ",
            "roles": [" Engineer ", "Engineer"],
            "countries": ["United Kingdom"],
            "skills": ["Python", "Python"],
            "work_modes": ["remote"],
            "salary_min": 100000,
            "salary_currency": "gbp",
        })
        self.assertEqual(profile.full_name, "Ada Lovelace")
        self.assertEqual(profile.roles, ("Engineer",))
        self.assertEqual(profile.skills, ("Python",))
        self.assertEqual(profile.salary_currency, "GBP")
        self.assertEqual(profile.to_dict()["roles"], ["Engineer"])
        self.assertEqual(CandidateProfile.from_dict(profile.to_dict()), profile)

    def test_rejects_incomplete_salary_and_invalid_mode(self) -> None:
        base = {"full_name": "Ada", "roles": ["Engineer"], "countries": ["UK"]}
        with self.assertRaisesRegex(ValueError, "set together"):
            CandidateProfile.from_dict(base | {"salary_min": 100000})
        with self.assertRaisesRegex(ValueError, "work_modes"):
            CandidateProfile.from_dict(base | {"work_modes": ["anywhere"]})
