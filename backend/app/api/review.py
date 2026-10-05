import html
import re
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from app.applications.queue import refresh_queue
from app.infrastructure.persistence.postgres import Repository
from app.vacancies.models import SEARCH_TRACKS

_MAX_BODY_BYTES = 64_000
_STATUS_LABELS = {
    "draft": "Черновик",
    "approved": "Утверждено · не отправлено",
    "rejected": "Отклонено",
    "sending": "Отправка начата · результат неизвестен",
    "sent": "Отправлено",
    "uncertain": "Результат отправки неизвестен · не повторять автоматически",
}


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _vacancy_link(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return _escape(url)
    return f'<a href="{_escape(url)}" target="_blank" rel="noopener noreferrer">Открыть вакансию ↗</a>'


def render_page(rows: list[dict[str, Any]], csrf_token: str, notice: str = "",
                track: str = "belarus") -> str:
    if track not in SEARCH_TRACKS:
        raise ValueError("invalid search track")
    track_input = f'<input type="hidden" name="track" value="{track}">'
    cards: list[str] = []
    for row in rows:
        review_id = int(row["id"])
        status = row.get("delivery_status") or row["status"]
        warnings = "".join(f"<li>{_escape(item)}</li>" for item in row["warnings"])
        reasons = "".join(f"<li>{_escape(item)}</li>" for item in row["reasons"])
        controls = ""
        if status == "draft" and row["is_active"]:
            controls = f'''
                <label class="pick"><input type="checkbox" name="id" value="{review_id}" form="approve-form"> Выбрать для утверждения</label>
                <form method="post" action="/edit/{review_id}">
                    <input type="hidden" name="csrf" value="{_escape(csrf_token)}">
                    {track_input}
                    <label for="draft-{review_id}">Текст отклика</label>
                    <textarea id="draft-{review_id}" name="draft" maxlength="10000" required>{_escape(row['draft_text'])}</textarea>
                    <button type="submit">Сохранить текст</button>
                </form>
                <form method="post" action="/reject/{review_id}" class="reject">
                    <input type="hidden" name="csrf" value="{_escape(csrf_token)}">
                    {track_input}
                    <button type="submit">Отклонить</button>
                </form>'''
        else:
            controls = f'<div class="saved-draft"><strong>Текст отклика:</strong><pre>{_escape(row["draft_text"])}</pre></div>'
            if status == "approved" and row["is_active"]:
                controls += f'''<form method="post" action="/recipient/{review_id}">
                    <input type="hidden" name="csrf" value="{_escape(csrf_token)}">
                    {track_input}
                    <label for="recipient-{review_id}">Опубликованный email работодателя</label>
                    <input id="recipient-{review_id}" type="email" name="recipient" maxlength="254"
                           value="{_escape(row.get('recipient_email') or '')}" required>
                    <button type="submit">Сохранить адрес</button>
                    <p class="warning">Указывай только адрес, опубликованный для этой вакансии. Сохранение адреса не отправляет письмо.</p>
                </form>'''
            if status == "rejected" and row["is_active"]:
                controls += f'''<form method="post" action="/restore/{review_id}">
                    <input type="hidden" name="csrf" value="{_escape(csrf_token)}">
                    {track_input}
                    <button type="submit">Вернуть в черновики</button>
                </form>'''
        stale = '<p class="warning">Вакансия закрыта — утверждение недоступно.</p>' if not row["is_active"] else ""
        source_note = '<p class="warning">Источник: <a href="https://himalayas.app/" target="_blank" rel="noopener noreferrer">Himalayas</a>. Условия проверяй у работодателя.</p>' if row.get("source") == "himalayas" else ""
        cards.append(f'''
            <article class="card">
                <div class="top"><span class="status {status}">{_escape(_STATUS_LABELS[status])}</span><span class="score">Оценка {int(row['score'])}/100</span></div>
                <h2>{_escape(row['title'])}</h2>
                <p class="meta">{_escape(row['company'])} · {_escape(row['location'])}</p>
                <p>{_vacancy_link(str(row['url']))}</p>
                {source_note}
                {stale}
                <details><summary>Почему подобрана · условия проверки</summary><div class="detail-grid"><div><h3>Совпадения</h3><ul>{reasons}</ul></div><div><h3>Проверить вручную</h3><ul>{warnings}</ul></div></div></details>
                <details><summary>Описание вакансии</summary><p class="description">{_escape(row['description'])}</p></details>
                {controls}
            </article>''')
    cards_html = "".join(cards) if cards else '<p class="empty">Очередь пуста. Импортируйте вакансии и нажмите «Обновить очередь».</p>'
    notice_html = f'<p class="notice" role="status">{_escape(notice)}</p>' if notice else ""
    draft_ids = sum(row["status"] == "draft" and row["is_active"] for row in rows)
    return f'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Очередь откликов</title><style>
:root {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #17253d; background: #f4f7fb; }}
body {{ margin: 0; }} main {{ max-width: 1040px; margin: auto; padding: 28px 18px 70px; }}
h1 {{ margin: 0 0 8px; font-size: 2rem; }} h2 {{ margin: 12px 0 4px; }} h3 {{ font-size: .92rem; margin: 0 0 8px; }}
.subtitle,.meta {{ color: #5d6a7d; }} .toolbar {{ display:flex; gap:12px; flex-wrap:wrap; align-items:center; margin: 24px 0; }}
button {{ background:#1157ad; color:white; border:0; border-radius:8px; padding:10px 16px; font-weight:600; cursor:pointer; }}
button:hover {{ background:#0d4489; }} .secondary button {{ background:#e7eef9; color:#144783; }}
.card {{ background:white; border:1px solid #dce5f1; border-radius:14px; padding:22px; margin:16px 0; box-shadow:0 3px 16px #1d41620d; }}
.top {{ display:flex; gap:12px; align-items:center; justify-content:space-between; }} .status {{ font-size:.8rem; padding:5px 9px; border-radius:30px; background:#e8f1ff; }}
.status.approved {{ background:#e4f6e8; }} .status.rejected {{ background:#f1e9e9; }} .score {{ color:#2767b5; font-weight:700; }}
details {{ border-top:1px solid #e5ebf2; padding:12px 0; }} summary {{ cursor:pointer; font-weight:600; }}
.detail-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; }} ul {{ padding-left:20px; margin-bottom:0; }} li {{ margin:6px 0; }}
.description {{ white-space:pre-wrap; line-height:1.5; }} textarea {{ box-sizing:border-box; width:100%; min-height:150px; padding:12px; font:inherit; border:1px solid #b9c8dc; border-radius:8px; margin:8px 0; }}
input[type=email] {{ box-sizing:border-box; width:100%; padding:10px; font:inherit; border:1px solid #b9c8dc; border-radius:8px; margin:8px 0; }}
.pick {{ display:block; margin:14px 0; font-weight:600; }} .reject {{ display:inline-block; margin-left:8px; }} .reject button {{ background:#f1e9e9; color:#7f2929; }}
.saved-draft pre {{ white-space:pre-wrap; font:inherit; }} .warning {{ color:#9a4a05; }} .notice {{ background:#e2f4e7; padding:12px; border-radius:8px; }}
.empty {{ padding:28px; background:white; border-radius:12px; }} .safety {{ background:#fff4dc; padding:12px; border-radius:8px; }}
.tabs {{ display:flex; gap:8px; margin:20px 0; flex-wrap:wrap; }} .tabs a {{ padding:10px 15px; border-radius:8px; color:#144783; background:#e7eef9; text-decoration:none; font-weight:600; }}
.tabs a[aria-current="page"] {{ color:white; background:#1157ad; }}
@media(max-width:680px) {{ .detail-grid {{ grid-template-columns:1fr; }} }}
</style></head><body><main>
<h1>Очередь откликов</h1><p class="subtitle">Подбор и редактирование перед отправкой</p>
<p class="safety">Утверждение меняет только статус в локальной базе. Отклик не отправляется работодателю.</p>
<nav class="tabs" aria-label="Направление поиска">
<a href="/?track=belarus" {'aria-current="page"' if track == 'belarus' else ''}>В Беларуси</a>
<a href="/?track=international" {'aria-current="page"' if track == 'international' else ''}>За рубежом · удалённо</a>
</nav>
<p class="subtitle">{'Вакансии, которые источник относит к удалённой работе из Беларуси. Условия подтверждайте у работодателя.' if track == 'belarus' else 'Международный поиск. Возможность работать из Беларуси проверяйте у работодателя; страна компании не подтверждена автоматически.'}</p>
{notice_html}
<div class="toolbar">
<form method="post" action="/refresh" class="secondary"><input type="hidden" name="csrf" value="{_escape(csrf_token)}">{track_input}<button type="submit">Обновить очередь</button></form>
<form method="post" action="/approve" id="approve-form"><input type="hidden" name="csrf" value="{_escape(csrf_token)}">{track_input}<label class="pick"><input type="checkbox" name="checked" value="yes" required> Я проверил условия вакансий</label><button type="submit">Утвердить выбранные ({draft_ids} доступны)</button></form>
</div>{cards_html}</main></body></html>'''


def _ids(values: list[str]) -> list[int]:
    if not values or len(values) > 100 or any(not re.fullmatch(r"[1-9]\d*", value) for value in values):
        raise ValueError("select 1 to 100 draft items")
    result = [int(value) for value in values]
    if len(set(result)) != len(result):
        raise ValueError("duplicate draft ids are not allowed")
    return result


def make_handler(repository: Repository, csrf_token: str):
    class ReviewHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return  # Local drafts and form actions should not be echoed into terminal logs.

        def _host_is_valid(self) -> bool:
            expected = f"127.0.0.1:{self.server.server_port}"
            return self.headers.get("Host") == expected

        def _headers(self, code: int, content_type: str, size: int) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()

        def _respond(self, code: int, body: str) -> None:
            payload = body.encode("utf-8")
            self._headers(code, "text/html; charset=utf-8", len(payload))
            self.wfile.write(payload)

        def _redirect(self, notice: str, track: str) -> None:
            self.send_response(303)
            self.send_header("Location", "/?track=" + track + "&notice=" + quote(notice))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def do_GET(self) -> None:
            if not self._host_is_valid():
                self._respond(400, "Invalid host")
                return
            path = urlsplit(self.path)
            if path.path != "/":
                self._respond(404, "Not found")
                return
            notice = parse_qs(path.query).get("notice", [""])[0][:200]
            track = parse_qs(path.query).get("track", ["belarus"])[0]
            if track not in SEARCH_TRACKS:
                self._respond(400, "Invalid search track")
                return
            try:
                self._respond(200, render_page(repository.list_reviews(track=track), csrf_token, notice, track))
            except Exception:
                self._respond(500, "Could not load the review queue")

        def do_POST(self) -> None:
            if not self._host_is_valid():
                self._respond(400, "Invalid host")
                return
            origin = self.headers.get("Origin")
            if origin and origin != f"http://127.0.0.1:{self.server.server_port}":
                self._respond(403, "Invalid origin")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= _MAX_BODY_BYTES or self.headers.get("Content-Type", "").split(";")[0] != "application/x-www-form-urlencoded":
                    raise ValueError("invalid form submission")
                data = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
                token = data.get("csrf", [""])[0]
                if not secrets.compare_digest(token, csrf_token):
                    self._respond(403, "Invalid form token")
                    return
                track = data.get("track", ["belarus"])[0]
                if track not in SEARCH_TRACKS:
                    raise ValueError("invalid search track")
                path = urlsplit(self.path).path
                if path == "/refresh":
                    result = refresh_queue(repository, track=track)
                    notice = f"Найдено подходящих: {result['matching']}; новых черновиков: {result['added']}."
                elif path == "/approve":
                    if data.get("checked") != ["yes"]:
                        raise ValueError("confirm that you checked the vacancy requirements")
                    count = repository.approve_reviews(_ids(data.get("id", [])))
                    notice = f"Утверждено: {count}. Ничего не отправлено."
                elif match := re.fullmatch(r"/edit/([1-9]\d*)", path):
                    draft = data.get("draft", [""])[0]
                    if not repository.edit_review(int(match.group(1)), draft):
                        raise ValueError("draft is no longer editable")
                    notice = "Текст черновика сохранён."
                elif match := re.fullmatch(r"/recipient/([1-9]\d*)", path):
                    recipient = data.get("recipient", [""])[0]
                    if not repository.set_review_recipient(int(match.group(1)), recipient):
                        raise ValueError("recipient is no longer editable")
                    notice = "Адрес работодателя сохранён. Письмо не отправлено."
                elif match := re.fullmatch(r"/reject/([1-9]\d*)", path):
                    if not repository.reject_review(int(match.group(1))):
                        raise ValueError("draft is no longer rejectable")
                    notice = "Черновик отклонён."
                elif match := re.fullmatch(r"/restore/([1-9]\d*)", path):
                    if not repository.restore_review(int(match.group(1))):
                        raise ValueError("review cannot be restored")
                    notice = "Черновик восстановлен."
                else:
                    self._respond(404, "Not found")
                    return
                self._redirect(notice, track)
            except (UnicodeDecodeError, ValueError) as exc:
                self._respond(400, _escape(exc))
            except Exception:
                self._respond(500, "Could not update the review queue")

    return ReviewHandler


def serve(repository: Repository, port: int = 8765) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(repository, secrets.token_urlsafe(32)))
    print(f"Review queue: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
