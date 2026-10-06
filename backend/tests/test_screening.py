import unittest
from dataclasses import replace
from unittest.mock import Mock

from app.candidate.profile import CandidateProfile
from app.matching.screening import screen, screen_title
from app.vacancies.models import Vacancy


def profile(**changes):
    return CandidateProfile.from_dict({"full_name": "Example", "roles": ["Automation Engineer"],
        "countries": ["Europe"], "skills": ["n8n", "REST API", "Go"], "work_modes": ["remote"],
        "residence_country": "Belarus"} | changes)


def job(**changes):
    return replace(Vacancy("ashby", "example", "1", "Example", "Automation Engineer", "Remote - Worldwide",
        "Build n8n workflows with REST APIs to automate lead intake, deduplicate records and integrate CRM services.",
        "https://jobs.ashbyhq.com/example/1", None), **changes)


class ScreeningTests(unittest.TestCase):
    def test_worldwide_and_explicit_belarus_are_matched_by_text(self):
        for location in ("Remote - Worldwide", "Remote - Belarus", "Remote - Anywhere", "Remote, Global"):
            with self.subTest(location=location):
                self.assertEqual(screen(profile(), job(location=location)).status, "matched")

    def test_europe_emea_and_unscoped_remote_need_review(self):
        for location in ("Remote - Europe", "Remote - EMEA", "Remote", "Remote · hh publication region: Belarus"):
            with self.subTest(location=location):
                self.assertEqual(screen(profile(), job(location=location)).status, "manual")

    def test_non_target_and_senior_titles_are_rejected(self):
        for title in ("Backend Engineer", "Senior Automation Engineer", "Lead Automation Engineer",
                      "QA Automation Engineer", "PLC Automation Engineer", "Automation Manager"):
            with self.subTest(title=title):
                self.assertEqual(screen(profile(), job(title=title)).status, "rejected")
        self.assertIsNone(screen_title(profile(), "Automation Engineer"))

    def test_scope_conflicts_and_exclusions_override_worldwide(self):
        for restriction in ("Applicants must be based in Poland.", "EU residents only.",
                            "You must reside in the European Union.", "Only hiring within the EEA.",
                            "This position is open to candidates located in Germany.",
                            "Work from anywhere except Belarus.", "Кандидаты из Беларуси не рассматриваются.",
                            "Нанимаем удалённо, кроме Беларуси."):
            with self.subTest(restriction=restriction):
                result = screen(profile(), job(description=job().description + "\n" + restriction))
                self.assertEqual(result.status, "rejected")
        self.assertEqual(screen(profile(), job(location="Remote - Worldwide excluding Belarus")).status, "rejected")

    def test_country_only_remote_cannot_pass(self):
        for location in ("Remote - UK", "Remote - Poland", "Remote - US", "Remote - Portugal"):
            with self.subTest(location=location):
                self.assertEqual(screen(profile(), job(location=location)).status, "rejected")

    def test_work_permits_are_not_inferred_even_when_belarus_is_named(self):
        result = screen(profile(), job(description=job().description + "\nApplicants must have work authorization in Belarus."))
        self.assertEqual(result.status, "manual")

    def test_team_country_and_eu_time_zone_are_not_residence_constraints(self):
        result = screen(profile(), job(description=job().description +
            "\nOur team includes EU colleagues. Collaborate during European time zones."))
        self.assertEqual(result.status, "matched")

    def test_incomplete_description_skills_and_work_mode_are_held(self):
        self.assertEqual(screen(profile(), job(description="n8n REST API")).status, "manual")
        self.assertEqual(screen(profile(skills=["n8n"]), job()).status, "manual")
        self.assertEqual(screen(profile(), job(location="Remote / Hybrid - Worldwide")).status, "manual")
        self.assertEqual(screen(profile(), job(location="Europe", description=job().description + " Fully remote.")).status, "manual")
        self.assertEqual(screen(profile(), job(location="On-site - Europe")).status, "rejected")

    def test_skills_in_title_alone_are_not_description_evidence(self):
        text = "Design and maintain business processes and workflow automation for customers across our global organisation."
        self.assertEqual(screen(profile(), job(title="n8n REST API Automation Engineer", description=text)).status, "manual")

    def test_generic_test_and_industrial_descriptions_are_rejected(self):
        for text in ("Maintain Selenium and Cypress test automation suites and browser regression checks for our QA department.",
                     "Maintain PLC and SCADA systems for industrial control equipment and instrumentation at our factories."):
            with self.subTest(text=text):
                self.assertEqual(screen(profile(), job(description=text)).status, "rejected")

    def test_hh_region_is_never_employer_hiring_confirmation(self):
        self.assertEqual(screen(profile(), job(source="hh", location="Remote - Belarus")).status, "manual")

    def test_aggregator_inferred_worldwide_needs_description_scope_evidence(self):
        for source in ("himalayas", "hh"):
            with self.subTest(source=source):
                self.assertEqual(screen(profile(), job(source=source)).status, "manual")
                self.assertEqual(screen(profile(), job(source=source,
                    description=job().description + " Work from anywhere.")).status, "matched")

    def test_experience_requirements_are_not_invented_from_resume_dates(self):
        for phrase in ("At least 3 years of experience required.", "5+ years experience.", "Нужно 3 года опыта."):
            with self.subTest(phrase=phrase):
                self.assertEqual(screen(profile(), job(description=job().description + " " + phrase)).status, "manual")

    def test_salary_and_missing_residence_are_not_assumed(self):
        self.assertEqual(screen(profile(residence_country=None), job()).status, "manual")
        self.assertEqual(screen(profile(salary_min=50000, salary_currency="EUR"), job()).status, "manual")

    def test_screening_paginates_past_five_hundred_without_skipping_older_jobs(self):
        from app.vacancies.screening import screen_saved_vacancies
        template = job()
        rows = [{name: getattr(template, name) for name in template.__dataclass_fields__} | {"id": i}
                for i in range(1, 502)]
        repository = Mock()
        repository.screening_batch.side_effect = lambda after_id, limit: rows[after_id:after_id + limit]
        repository.save_screening.return_value = True
        self.assertEqual(screen_saved_vacancies(repository, profile())["matched"], 501)
        self.assertEqual([call.args for call in repository.screening_batch.call_args_list], [(0, 200), (200, 200), (400, 200)])


if __name__ == "__main__":
    unittest.main()
