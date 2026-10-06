import unittest

from app.candidate.profile import CandidateProfile
from app.matching.rules import evaluate
from app.templates.drafts import render_application, render_linkedin_message
from app.vacancies.models import Vacancy


def _profile(**overrides: object) -> CandidateProfile:
    data = {
        "full_name": "Example Candidate",
        "resume_text": "Built a Go API and n8n workflows.",
        "roles": ["AI Automation Engineer", "Automation Developer"],
        "countries": ["Europe"],
        "skills": ["Go", "n8n", "REST API"],
        "work_modes": ["remote"],
    }
    return CandidateProfile.from_dict(data | overrides)


def _job(**overrides: object) -> Vacancy:
    data = {
        "source": "greenhouse", "board_token": "example", "external_id": "1",
        "company": "Example Ltd", "title": "AI Automation Engineer",
        "location": "Remote - Europe", "description": "Build n8n and REST API integrations.",
        "url": "https://example.com/jobs/1", "source_updated_at": None,
    }
    return Vacancy(**(data | overrides))


class MatchingTests(unittest.TestCase):
    def test_russian_business_automation_is_matched_but_industrial_and_qa_are_not(self):
        profile = _profile(roles=["Automation Engineer"])
        self.assertEqual(evaluate(profile, _job(title="Инженер по автоматизации бизнес-процессов")).status, "review")
        self.assertEqual(evaluate(profile, _job(title="Инженер по автоматизации тестирования")).status, "rejected")
        self.assertEqual(evaluate(profile, _job(title="Industrial Automation Engineer PLC")).status, "rejected")

    def test_automation_engineer_focus_rejects_backend_titles_despite_shared_skills(self) -> None:
        profile = _profile(roles=["Automation Engineer"])
        backend = _job(title="Senior Backend Engineer", description="Build Go APIs and n8n workflows.")
        self.assertEqual(evaluate(profile, backend).status, "rejected")
        self.assertEqual(evaluate(profile, _job(title="Automation Engineer")).status, "review")
        self.assertEqual(evaluate(profile, _job(title="Automation Specialist")).status, "rejected")
        self.assertEqual(evaluate(profile, _job(title="Automation Developer")).status, "rejected")
        self.assertEqual(evaluate(profile, _job(title="Engineer, Automation Systems")).status, "review")

    def test_senior_automation_title_is_not_given_a_scoring_penalty(self) -> None:
        profile = _profile(roles=["Automation Engineer"])
        regular = evaluate(profile, _job(title="Automation Engineer"))
        senior = evaluate(profile, _job(title="Senior Automation Engineer"))
        self.assertEqual(senior.status, "review")
        self.assertEqual(senior.score, regular.score)
        self.assertTrue(any("required experience" in warning for warning in senior.warnings))

    def test_remote_europe_role_produces_ranked_review_and_factual_draft(self) -> None:
        profile, job = _profile(), _job()
        result = evaluate(profile, job)
        self.assertEqual(result.status, "review")
        self.assertEqual(result.score, 95)
        self.assertEqual(result.matched_skills, ("n8n", "REST API"))
        self.assertIn("country of residence", " ".join(result.warnings))
        draft = render_application(profile, job, result)
        self.assertIn("AI Automation Engineer", draft)
        self.assertIn("n8n, REST API", draft)
        self.assertNotIn("Go API", draft)  # Not mentioned in this vacancy.
        self.assertNotIn("5 years", draft)

    def test_new_draft_includes_profile_contact_email(self) -> None:
        profile, job = _profile(contact_email="candidate@example.com"), _job()
        draft = render_application(profile, job, evaluate(profile, job))
        self.assertTrue(draft.endswith("Example Candidate\ncandidate@example.com"))

    def test_linkedin_message_uses_only_matched_skills_and_is_not_an_email(self) -> None:
        profile, job = _profile(contact_email="candidate@example.com"), _job()
        message = render_linkedin_message(profile, job, evaluate(profile, job))
        self.assertIn("AI Automation Engineer opening at Example Ltd", message)
        self.assertIn("n8n, REST API", message)
        self.assertNotIn("Go", message)
        self.assertNotIn("candidate@example.com", message)
        with self.assertRaises(ValueError):
            render_linkedin_message(profile, _job(title="Sales Manager"),
                                    evaluate(profile, _job(title="Sales Manager")))

    def test_rejects_office_or_hybrid_when_remote_only(self) -> None:
        for location in ("Berlin, Germany", "Hybrid - Europe", "On-site - Europe"):
            with self.subTest(location=location):
                result = evaluate(_profile(), _job(location=location))
                self.assertEqual(result.status, "rejected")
                with self.assertRaises(ValueError):
                    render_application(_profile(), _job(location=location), result)

    def test_rejects_missing_remote_or_explicit_no_remote(self) -> None:
        self.assertEqual(evaluate(_profile(), _job(location="Europe", description="Build APIs.")).status,
                         "rejected")
        self.assertEqual(evaluate(_profile(), _job(description="This job is not remote.")).status,
                         "rejected")

    def test_description_only_remote_is_flagged_for_review(self) -> None:
        result = evaluate(_profile(), _job(location="Europe", description="Fully remote. Build n8n workflows."))
        self.assertEqual(result.status, "review")
        self.assertTrue(any("only in the description" in warning for warning in result.warnings))

    def test_ambiguous_remote_hybrid_is_not_silently_accepted(self) -> None:
        result = evaluate(_profile(), _job(location="Remote / Hybrid - Europe"))
        self.assertEqual(result.status, "review")
        self.assertTrue(any("hybrid" in warning for warning in result.warnings))

    def test_home_office_is_treated_as_remote(self) -> None:
        self.assertEqual(evaluate(_profile(), _job(location="Home Office - Europe")).status, "review")

    def test_us_and_canada_only_remote_is_rejected_for_europe_target(self) -> None:
        result = evaluate(_profile(), _job(location="Remote, Canada; Remote, United States"))
        self.assertEqual(result.status, "rejected")
        self.assertIn("outside", result.reasons[0])
        self.assertEqual(evaluate(_profile(), _job(location="Remote, Canada; Remote, Europe")).status,
                         "review")
        self.assertEqual(evaluate(_profile(), _job(location="Remote - Australia / Europe")).status,
                         "review")

    def test_country_specific_remote_jobs_are_rejected_when_residence_is_not_listed(self) -> None:
        profile = _profile(residence_country="Belarus")
        self.assertEqual(evaluate(profile, _job(location="Remote, Canada; Remote, Poland")).status,
                         "rejected")
        self.assertEqual(evaluate(profile, _job(location="Remote - Portugal")).status, "rejected")
        self.assertEqual(evaluate(profile, _job(location="Remote - Belarus")).status, "review")
        self.assertEqual(evaluate(profile, _job(location="Remote - Europe; Remote - US")).status,
                         "review")

    def test_explicit_residence_exclusion_in_description_rejects_remote_role(self) -> None:
        profile = _profile(residence_country="Belarus")
        result = evaluate(profile, _job(location="Remote - Worldwide",
                                        description="Work from anywhere, except Russia, Belarus, and China."))
        self.assertEqual(result.status, "rejected")
        self.assertIn("Belarus", result.reasons[0])
        allowed = evaluate(profile, _job(location="Remote - Worldwide",
                                         description="Our distributed team includes colleagues in Belarus."))
        self.assertEqual(allowed.status, "review")

    def test_rejects_unrelated_role(self) -> None:
        self.assertEqual(evaluate(_profile(), _job(title="Sales Manager")).status, "rejected")
        self.assertEqual(evaluate(_profile(), _job(title="AI Sales Manager")).status, "rejected")

    def test_rejects_qa_and_management_automation_outside_target_roles(self) -> None:
        self.assertEqual(evaluate(_profile(), _job(title="Senior QA Automation Engineer")).status, "rejected")
        self.assertEqual(evaluate(_profile(), _job(title="CRM Automation Manager")).status, "rejected")
        self.assertEqual(evaluate(_profile(roles=["QA Automation Engineer"]),
                                  _job(title="QA Automation Engineer")).status, "review")

    def test_go_skill_does_not_match_ordinary_verb_and_synonyms_are_deduplicated(self) -> None:
        profile = _profile(skills=["Go", "Golang", "REST API", "Webhooks", "LLM"])
        job = _job(description="Please go to our site. Build REST APIs, webhooks and LLM tools.")
        result = evaluate(profile, job)
        self.assertEqual(result.matched_skills, ("REST API", "Webhooks", "LLM"))
        go_result = evaluate(profile, _job(description="Build Go and Golang services."))
        self.assertEqual(go_result.matched_skills, ("Go",))

    def test_partial_role_and_unconfirmed_region_still_need_review(self) -> None:
        result = evaluate(_profile(salary_min=70000, salary_currency="EUR", residence_country="Belarus"),
                          _job(title="Senior Automation Developer", location="Remote - Worldwide"))
        self.assertEqual(result.status, "review")
        self.assertTrue(any("target region" in warning for warning in result.warnings))
        self.assertTrue(any("Salary" in warning for warning in result.warnings))
        self.assertTrue(any("Belarus" in warning for warning in result.warnings))
        self.assertTrue(any("Senior" in warning for warning in result.warnings))

    def test_draft_does_not_include_unsupported_profile_summary(self) -> None:
        profile = _profile(resume_text="Claim that must not be copied automatically.", skills=[])
        job = _job(description="Automate processes.")
        result = evaluate(profile, job)
        draft = render_application(profile, job, result)
        self.assertNotIn("Claim that must not be copied", draft)
        self.assertNotIn("My background includes", draft)


if __name__ == "__main__":
    unittest.main()
