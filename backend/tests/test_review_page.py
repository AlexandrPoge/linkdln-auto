import http.client
import io
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock
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
    def test_post_route_checks_token_and_confirmation_without_a_socket(self) -> None:
        repository = Mock()
        repository.approve_reviews.return_value = 1
        handler_class = make_handler(repository, "test-token")

        def submit(form: dict[str, str]) -> int:
            body = urlencode(form).encode()
            handler = object.__new__(handler_class)
            handler.server = SimpleNamespace(server_port=8765)
            handler.headers = {"Host": "127.0.0.1:8765", "Content-Length": str(len(body)),
                               "Content-Type": "application/x-www-form-urlencoded"}
            handler.path = "/approve"
            handler.rfile = io.BytesIO(body)
            handler.wfile = io.BytesIO()
            handler.send_response = Mock()
            handler.send_header = Mock()
            handler.end_headers = Mock()
            handler.do_POST()
            return handler.send_response.call_args.args[0]

        self.assertEqual(submit({"csrf": "wrong", "id": "7", "checked": "yes"}), 403)
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
        self.assertIn("Отклик не отправляется", page)
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

    def test_approved_item_is_not_editable(self) -> None:
        row = _review() | {"status": "approved"}
        page = render_page([row], "secret")
        self.assertNotIn('action="/edit/7"', page)
        self.assertIn('action="/recipient/7"', page)
        self.assertIn("не отправлено", page)

    def test_delivery_status_is_not_mistaken_for_approval(self) -> None:
        page = render_page([_review() | {"status": "approved", "delivery_status": "uncertain"}], "secret")
        self.assertIn("Результат отправки неизвестен", page)
        self.assertNotIn("Утверждено · не отправлено", page)

    def test_rejected_item_can_be_restored(self) -> None:
        page = render_page([_review() | {"status": "rejected"}], "secret")
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
