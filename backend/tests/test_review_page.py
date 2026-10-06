import http.client
import io
import threading
from contextlib import nullcontext
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from app.api.review import make_handler, render_page
from app.candidate.profile import CandidateProfile


def _review() -> dict:
    return {
        "id": 7, "status": "draft", "score": 85, "is_active": True,
        "title": "Automation <Engineer>", "company": "Example & Co", "location": "Remote - Europe",
        "description": "Build <script>alert(1)</script> workflows.",
        "url": "https://example.com/job/7", "reasons": ["n8n matched"],
        "warnings": ["Check residence"], "draft_text": "Hello <team> & friends",
    }


class ReviewPageTests(unittest.TestCase):
    def test_referrer_policy_preserves_webkit_form_origin_but_hides_callback(self):
        handler_class = make_handler(Mock(), "test-token")
        for path, policy in (("/", "same-origin"), ("/gmail-callback?code=private", "no-referrer")):
            handler = object.__new__(handler_class)
            handler.path = path
            handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
            handler._headers(200, "text/html", 0)
            handler.send_header.assert_any_call("Referrer-Policy", policy)

    def test_oauth_routes_check_csrf_and_callback_pauses_sending(self):
        from app.applications.automation import AutomationSettings
        repository, automation = Mock(), Mock()
        repository.automation_lock.return_value = nullcontext(True)
        automation.store.load.return_value = AutomationSettings("example@gmail.com", "a" * 16)
        handler_class = make_handler(repository, "test-token", automation=automation)

        def handler(path, body=b""):
            value = object.__new__(handler_class)
            value.server = SimpleNamespace(server_port=8765)
            value.headers = {"Host": "127.0.0.1:8765", "Content-Length": str(len(body)),
                             "Content-Type": "application/x-www-form-urlencoded"}
            value.path, value.rfile, value.wfile = path, io.BytesIO(body), io.BytesIO()
            value.send_response, value.send_header, value.end_headers = Mock(), Mock(), Mock()
            return value

        bad = handler("/gmail-client", urlencode({"csrf": "wrong", "client_json": "private"}).encode())
        bad.do_POST()
        self.assertEqual(bad.send_response.call_args.args[0], 403)
        automation.oauth.configure.assert_not_called()
        good = handler("/gmail-client", urlencode({"csrf": "test-token", "client_json": "private"}).encode())
        good.do_POST()
        self.assertEqual(good.send_response.call_args.args[0], 303)
        automation.oauth.configure.assert_called_once_with("private")
        self.assertNotIn("private", str(good.send_header.call_args_list))

        def finish(state, code, *, error, on_connected):
            self.assertEqual((state, code, error), ("state", "private-code", False))
            on_connected("verified@gmail.com")
            return "international", "verified@gmail.com"
        automation.oauth.finish.side_effect = finish
        callback = handler("/gmail-callback?state=state&code=private-code")
        callback.do_GET()
        self.assertEqual(callback.send_response.call_args.args[0], 303)
        saved = automation.store.save.call_args.args[0]
        self.assertEqual(saved.email, "verified@gmail.com")
        self.assertFalse(saved.password)
        self.assertFalse(saved.notifications or saved.mailbox or saved.send_applications or saved.auto_approve)
        self.assertNotIn("private-code", str(callback.send_header.call_args_list))

    def test_oauth_panel_never_renders_tokens_or_password_fields(self):
        state = {"connected": False, "email": "example@gmail.com", "resume_path": "", "daily_limit": 5,
                 "notifications": True, "mailbox": True, "send_applications": True, "auto_approve": True,
                 "sent_total": 0, "notification_pending": 1, "uncertain_total": 0,
                 "oauth": {"connected": False, "client_configured": False}}
        page = render_page([], "csrf", automation_state=state)
        self.assertIn("OAuth-клиент: не настроен", page)
        self.assertIn("Сохранить настройки · продолжить без почты", page)
        self.assertNotIn('name="password"', page)
        self.assertIn('action="/gmail-connect"', page)
        self.assertIn("7 дней", page)

    def test_screening_dashboard_explains_held_items_and_collapses_mailbox_noise(self):
        state = {"connected": False, "email": "", "resume_path": "", "daily_limit": 5,
                 "notifications": True, "mailbox": True, "send_applications": True, "auto_approve": True,
                 "sent_total": 0, "notification_pending": 1, "notification_held": 164, "uncertain_total": 0,
                 "oauth": {"connected": False, "client_configured": False},
                 "screening_counts": {"matched": 1, "manual": 2, "rejected": 4},
                 "alert_screening": {"manual": 8, "rejected": 10},
                 "leads": [{"source": "linkedin", "title": "Backend <Engineer>", "url": "https://example.com/1",
                            "screening_status": "rejected", "screening_reason": "Unverified alert title: Job title does not match target roles."}]}
        page = render_page([], "csrf", automation_state=state)
        self.assertIn("Подходит по тексту: 1 · Нужна проверка: 2 · Отсеяно: 4", page)
        self.assertIn("удержано от отправки: 164", page)
        self.assertIn('<details><summary>Ссылки из почтовых уведомлений', page)
        self.assertIn("По заголовку письма: Название не совпадает", page)
        self.assertIn("Backend &lt;Engineer&gt;", page)

    @patch("app.api.review.verify_gmail")
    def test_gmail_form_checks_csrf_then_verifies_without_echoing_password(self, verify):
        from app.applications.automation import AutomationSettings
        repository, runner, automation = Mock(), Mock(), Mock()
        automation.oauth = None
        automation.store.load.return_value = AutomationSettings()
        runner.trigger.return_value = True
        handler_class = make_handler(repository, "test-token", runner, automation)

        def submit(token):
            body = urlencode({"csrf": token, "email": "example@gmail.com", "password": "a" * 16,
                              "daily_limit": "5", "notifications": "yes", "mailbox": "yes"}).encode()
            handler = object.__new__(handler_class)
            handler.server = SimpleNamespace(server_port=8765)
            handler.headers = {"Host": "127.0.0.1:8765", "Content-Length": str(len(body)),
                               "Content-Type": "application/x-www-form-urlencoded"}
            handler.path = "/automation-settings"
            handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
            handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
            handler.do_POST()
            return handler

        bad = submit("wrong")
        self.assertEqual(bad.send_response.call_args.args[0], 403)
        verify.assert_not_called()
        good = submit("test-token")
        self.assertEqual(good.send_response.call_args.args[0], 303)
        verify.assert_called_once_with("example@gmail.com", "a" * 16)
        automation.store.save.assert_called_once()
        runner.trigger.assert_called_once()
        self.assertNotIn("a" * 16, str(good.send_header.call_args_list))

    def test_post_route_checks_token_and_confirmation_without_a_socket(self) -> None:
        repository = Mock()
        repository.approve_reviews.return_value = 1
        handler_class = make_handler(repository, "test-token")

        def submit(form: dict[str, str], origin=None) -> int:
            body = urlencode(form).encode()
            handler = object.__new__(handler_class)
            handler.server = SimpleNamespace(server_port=8765)
            handler.headers = {"Host": "127.0.0.1:8765", "Content-Length": str(len(body)),
                               "Content-Type": "application/x-www-form-urlencoded"}
            handler.path = "/approve"
            if origin is not None:
                handler.headers["Origin"] = origin
            handler.rfile = io.BytesIO(body)
            handler.wfile = io.BytesIO()
            handler.send_response = Mock()
            handler.send_header = Mock()
            handler.end_headers = Mock()
            handler.do_POST()
            return handler.send_response.call_args.args[0]

        self.assertEqual(submit({"csrf": "wrong", "id": "7", "checked": "yes"}), 403)
        for origin in ("null", "https://evil.test", "http://127.0.0.1:9999"):
            self.assertEqual(submit({"csrf": "test-token", "id": "7", "checked": "yes"}, origin), 403)
        self.assertEqual(submit({"csrf": "test-token", "id": "7"}), 400)
        repository.approve_reviews.assert_not_called()
        self.assertEqual(submit({"csrf": "test-token", "id": "7", "checked": "yes"}), 303)
        repository.approve_reviews.assert_called_once_with([7])

    def test_page_escapes_external_data_and_explains_approval(self) -> None:
        page = render_page([_review()], "secret")
        self.assertIn("Automation &lt;Engineer&gt;", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("Hello &lt;team&gt; &amp; friends", page)
        self.assertNotIn("<script>", page)
        self.assertIn("если Gmail подключён и отправка включена", page)
        self.assertIn('href="/?track=belarus"', page)
        self.assertIn('href="/?track=international"', page)
        self.assertIn('name="track" value="belarus"', page)

    def test_international_tab_keeps_context_in_forms(self) -> None:
        page = render_page([_review()], "secret", track="international")
        self.assertIn('name="track" value="international"', page)
        self.assertIn("Возможность работать из Беларуси", page)
        with self.assertRaisesRegex(ValueError, "invalid search track"):
            render_page([], "secret", track="other")

    def test_aggregator_attribution_is_visible(self) -> None:
        page = render_page([_review() | {"source": "himalayas"}], "secret")
        self.assertIn("Источник: <a href=\"https://himalayas.app/\"", page)

    def test_page_shows_unsent_linkedin_message_for_matching_active_job(self) -> None:
        profile = CandidateProfile.from_dict({
            "full_name": "Example Candidate", "roles": ["Automation Engineer"],
            "countries": ["Europe"], "skills": ["n8n"], "work_modes": ["remote"],
        })
        row = _review() | {"title": "Automation Engineer", "description": "Build n8n workflows."}
        page = render_page([row], "secret", profile=profile)
        self.assertIn("Сообщение рекрутеру для LinkedIn · не отправлено", page)
        self.assertIn("I work with n8n", page)
        self.assertIn("readonly", page)
        self.assertNotIn("Сообщение рекрутеру для LinkedIn · не отправлено",
                         render_page([row | {"is_active": False}], "secret", profile=profile))

    def test_dashboard_shows_filtered_jobs_and_search_status_when_queue_empty(self) -> None:
        profile = CandidateProfile.from_dict({
            "full_name": "Example Candidate", "roles": ["Automation Engineer"],
            "countries": ["Europe"], "skills": ["n8n"], "work_modes": ["remote"],
        })
        discovered = [{
            "source": "ashby", "board_token": "example", "external_id": "1",
            "title": "Sales <Manager>", "company": "Example & Co",
            "location": "Remote - Europe", "description": "Manage accounts.",
            "url": "https://example.com/jobs/1", "source_updated_at": None,
        }]
        status = {"running": False, "started_at": None, "finished_at": None,
                  "report": {"sources": [{"source": "ashby", "status": "ok", "new": 1}],
                             "successful": 1, "failed": 0},
                  "error": None, "interval_seconds": 3600}
        page = render_page([], "secret", profile=profile, discovered=discovered, search_status=status)
        self.assertIn("Что нашлось в источниках", page)
        self.assertIn("Sales &lt;Manager&gt;", page)
        self.assertIn("Example &amp; Co", page)
        self.assertIn("Отсеяно", page)
        self.assertIn("Руководящая позиция вне текущего поиска.", page)
        self.assertIn("новых записей: 1", page)
        self.assertIn('action="/search-now"', page)

    def test_search_now_requires_token_and_uses_runner(self) -> None:
        repository = Mock()
        runner = Mock()
        runner.trigger.return_value = True
        handler_class = make_handler(repository, "test-token", runner)

        def submit(token: str) -> int:
            body = urlencode({"csrf": token, "track": "international"}).encode()
            handler = object.__new__(handler_class)
            handler.server = SimpleNamespace(server_port=8765)
            handler.headers = {"Host": "127.0.0.1:8765", "Content-Length": str(len(body)),
                               "Content-Type": "application/x-www-form-urlencoded"}
            handler.path = "/search-now"
            handler.rfile = io.BytesIO(body)
            handler.wfile = io.BytesIO()
            handler.send_response = Mock()
            handler.send_header = Mock()
            handler.end_headers = Mock()
            handler.do_POST()
            return handler.send_response.call_args.args[0]

        self.assertEqual(submit("wrong"), 403)
        runner.trigger.assert_not_called()
        self.assertEqual(submit("test-token"), 303)
        runner.trigger.assert_called_once_with()

    def test_nearby_vacancies_are_visible_before_collapsed_irrelevant_results(self) -> None:
        profile = CandidateProfile.from_dict({
            "full_name": "Example Candidate", "roles": ["Automation Engineer"],
            "countries": ["Europe"], "skills": ["n8n"], "work_modes": ["remote"],
        })
        base = {"source": "ashby", "board_token": "example", "company": "Example",
                "location": "Remote - Europe", "description": "Build workflows.",
                "source_updated_at": None}
        jobs = [base | {"external_id": str(index), "title": f"Sales Manager {index}",
                        "url": f"https://example.com/{index}"} for index in range(10)]
        jobs.append(base | {"external_id": "11", "title": "Automation Engineer",
                            "url": "https://example.com/11"})
        page = render_page([], "secret", profile=profile, discovered=jobs)
        self.assertLess(page.index("Automation Engineer</h3>"), page.index("Отсеянные вакансии"))
        self.assertIn("Отсеяно", page)
        self.assertIn("Отсеянные вакансии (10)", page)

    def test_dashboard_navigation_uses_real_sections_and_no_external_assets(self):
        page = render_page([], "csrf")
        for section in ("overview", "opportunities", "queue", "sources", "settings"):
            self.assertIn(f'href="#{section}"', page)
            self.assertIn(f'id="{section}"', page)
        self.assertIn("flow / jobs", page)
        self.assertIn("@media(max-width:760px)", page)
        self.assertIn("prefers-reduced-motion", page)
        self.assertNotIn('<script', page)
        self.assertNotIn('fonts.googleapis', page)

    def test_source_and_stage_results_are_human_readable_not_python_dicts(self):
        status = {"running": False, "finished_at": None, "error": None, "interval_seconds": 21600,
                  "report": {"successful": 1, "failed": 1, "sources": [
                      {"source": "ashby:zapier", "status": "ok", "total": 9, "new": 1},
                      {"source": "hh:n8n", "status": "failed", "error": "api.hh.ru: HTTP 403"}],
                      "automation": {"mailbox": {"processed": 94, "new_links": 188},
                                     "applications": {"sent": 0, "uncertain": 0}, "digest": "disabled"}}}
        page = render_page([], "csrf", search_status=status)
        self.assertIn("Zapier · Ashby", page)
        self.assertIn("Источник вернул 403", page)
        self.assertIn("писем обработано: 94", page)
        self.assertIn("Этап выключен в настройках", page)
        self.assertNotIn("&#x27;processed&#x27;", page)

    def test_running_search_disables_only_extra_run_button(self):
        status = {"running": True, "finished_at": None, "error": None, "interval_seconds": 21600, "report": None}
        page = render_page([], "csrf", search_status=status)
        self.assertIn('disabled>Искать сейчас', page)
        self.assertIn("Идёт поиск", page)
        self.assertIn('name="csrf" value="csrf"', page)

    def test_overview_never_claims_disabled_mail_stages_are_running(self):
        from app.api.review import _overview
        state = {"connected": True, "send_applications": False, "notifications": False,
                 "auto_approve": False, "daily_limit": 5, "attempts_24h": 2,
                 "screening_counts": {"matched": 1}}
        panel = _overview(state, {}, "international")
        self.assertIn("отклики: выключены", panel)
        self.assertIn("Подборки: выключены", panel)
        self.assertIn("Отправка откликов выключена", panel)
        self.assertIn("2 <em>/ 5", panel)

    def test_discovered_jobs_are_deduplicated_with_reasons_preserved(self):
        from app.api.review import _discovered_vacancies
        from test_screening import profile, job
        vacancy = job()
        row = {name: getattr(vacancy, name) for name in vacancy.__dataclass_fields__}
        output, count = _discovered_vacancies([row, row | {"board_token": "another-query"}], profile())
        self.assertEqual(count, 1)
        self.assertEqual(output.count("Automation Engineer</h3>"), 1)

    def test_approved_item_is_not_editable(self) -> None:
        row = _review() | {"status": "approved"}
        page = render_page([row], "secret")
        self.assertNotIn('action="/edit/7"', page)
        self.assertIn('action="/recipient/7"', page)
        self.assertIn("не отправлено", page)

    def test_identical_query_drafts_are_collapsed_without_removing_controls(self):
        first = _review() | {"source": "himalayas", "external_id": "one-job"}
        second = first | {"id": 8}
        page = render_page([first, second], "secret")
        self.assertIn("Повторные черновики (1)", page)
        self.assertIn("Утвердить выбранные (1 доступны)", page)
        self.assertIn('action="/edit/7"', page)
        self.assertIn('action="/edit/8"', page)
        page = render_page([first, second | {"draft_text": "My edited text"}], "secret")
        self.assertNotIn("Повторные черновики", page)
        self.assertIn("My edited text", page)

    def test_delivery_status_is_not_mistaken_for_approval(self) -> None:
        page = render_page([_review() | {"status": "approved", "delivery_status": "uncertain"}], "secret")
        self.assertIn("Результат отправки неизвестен", page)
        self.assertNotIn("Утверждено · не отправлено", page)

    def test_rejected_item_can_be_restored(self) -> None:
        page = render_page([_review() | {"status": "rejected"}], "secret")
        self.assertIn("Отклонённые черновики (1)", page)
        self.assertIn('action="/restore/7"', page)
        self.assertNotIn('action="/edit/7"', page)

    def test_web_handler_checks_csrf_and_approves_only_local_status(self) -> None:
        repository = Mock()
        repository.list_reviews.return_value = [_review()]
        repository.approve_reviews.return_value = 1
        repository.set_review_recipient.return_value = True
        repository.get_profile.return_value = CandidateProfile.from_dict({
            "full_name": "Example", "roles": ["Automation Developer"], "countries": ["Europe"],
            "skills": ["n8n"], "work_modes": ["remote"],
        })
        repository.list_vacancies.return_value = []
        repository.enqueue_reviews.return_value = 0
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(repository, "test-token"))
        except PermissionError:
            self.skipTest("local TCP binding is unavailable in this sandbox")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", "/")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("Очередь откликов", response.read().decode())
            repository.list_reviews.assert_called_with(track="belarus")

            connection.request("GET", "/?track=international")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("За рубежом", response.read().decode())
            repository.list_reviews.assert_called_with(track="international")

            connection.request("GET", "/?track=wrong")
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()

            body = urlencode({"csrf": "test-token", "track": "international"})
            connection.request("POST", "/refresh", body, {"Content-Type": "application/x-www-form-urlencoded"})
            response = connection.getresponse()
            self.assertEqual(response.status, 303)
            self.assertIn("track=international", response.getheader("Location"))
            response.read()
            repository.list_vacancies.assert_called_with(500, track="international")

            body = urlencode({"csrf": "test-token", "track": "international",
                              "recipient": "jobs@example.com"})
            connection.request("POST", "/recipient/7", body,
                               {"Content-Type": "application/x-www-form-urlencoded"})
            response = connection.getresponse()
            self.assertEqual(response.status, 303)
            response.read()
            repository.set_review_recipient.assert_called_once_with(7, "jobs@example.com")

            body = urlencode({"csrf": "wrong", "id": "7"})
            connection.request("POST", "/approve", body, {"Content-Type": "application/x-www-form-urlencoded"})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            repository.approve_reviews.assert_not_called()

            body = urlencode({"csrf": "test-token", "id": "7", "checked": "yes"})
            connection.request("POST", "/approve", body, {"Content-Type": "application/x-www-form-urlencoded",
                                                          "Origin": f"http://127.0.0.1:{server.server_port}"})
            response = connection.getresponse()
            self.assertEqual(response.status, 303)
            response.read()
            repository.approve_reviews.assert_called_once_with([7])
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
