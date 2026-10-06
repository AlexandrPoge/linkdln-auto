import html
import re
import secrets
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from app.applications.queue import refresh_queue
from app.api.search_runner import SearchRunner
from app.applications.automation import AutomationPipeline, AutomationSettings
from app.integrations.mailbox import verify_gmail
from app.candidate.profile import CandidateProfile
from app.infrastructure.persistence.postgres import Repository
from app.matching.rules import evaluate
from app.templates.drafts import render_linkedin_message
from app.vacancies.models import SEARCH_TRACKS
from app.vacancies.models import Vacancy

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


def _display_reason(reason: str) -> str:
    translations = {
        "Management role is outside the target roles.": "Руководящая позиция вне текущего поиска.",
        "QA/test automation is outside the target roles.": "QA-автоматизация вне текущего поиска.",
        "Industrial automation is outside the target roles.": "Промышленная автоматизация вне текущего поиска.",
        "Job title does not match target roles.": "Название не совпадает с целевой ролью.",
        "Job title does not match the target role type.": "Тип роли не совпадает с целевой инженерной позицией.",
        "Vacancy explicitly excludes remote work.": "Вакансия исключает удалённую работу.",
        "Location is listed as office-based or hybrid.": "Вакансия офисная или гибридная.",
        "Remote work is not confirmed.": "Удалённая работа не подтверждена.",
        "Listed remote locations are outside the target European region.": "География удалённой работы вне целевого региона.",
    }
    if reason.startswith("Listed remote locations do not include"):
        return "Указанные страны удалённой работы не включают страну проживания."
    if reason.startswith("The vacancy explicitly excludes applicants from"):
        return "Вакансия исключает кандидатов из страны проживания."
    return translations.get(reason, reason)


def _search_status(search_status: dict[str, Any] | None) -> str:
    if search_status is None:
        return '<p class="muted">Автопоиск не настроен. Добавь локальный data/searches.json и перезапусти сервер.</p>'
    if search_status["running"]:
        summary = "Идёт поиск по источникам. Обнови страницу через минуту."
        style = "running"
    elif search_status["error"]:
        summary = f"Ошибка последнего запуска: {_escape(search_status['error'])}"
        style = "error"
    elif report := search_status["report"]:
        new_count = sum(int(item.get("new", 0)) for item in report["sources"] if item["status"] == "ok")
        summary = (f"Последний запуск · источников проверено: {report['successful']} · "
                   f"ошибок: {report['failed']} · новых вакансий: {new_count} · отправлено откликов: {report.get('sent', 0)}.")
        style = "ok" if not report["failed"] else "error"
    else:
        summary = "Первый поиск запустится при открытии сервера."
        style = "running"
    finished = search_status["finished_at"]
    when = datetime.fromisoformat(finished).astimezone().strftime("%d.%m.%Y %H:%M") if finished else "ещё не завершён"
    hours = search_status["interval_seconds"] // 3600
    interval = f"каждые {hours} ч" if hours else f"каждые {search_status['interval_seconds'] // 60} мин"
    report = search_status.get("report") or {}
    source_details = "".join(f'<li>{_escape(item["source"])} · {_escape(item["status"])} · '
                             f'{_escape(item.get("error") or item.get("note") or str(item.get("total", 0)) + " вакансий")}</li>'
                             for item in report.get("sources", []))
    stage_details = "".join(f'<li>{_escape(key)}: {_escape(value)}</li>' for key, value in report.get("automation", {}).items())
    return (f'<p class="run-status {style}" role="status">{summary}</p>'
            f'<p class="muted">Последняя проверка: {_escape(when)} · Повтор: {interval}, пока приложение запущено.</p>'
            f'<details><summary>Источники и результаты этапов</summary><ul>{source_details}{stage_details}</ul></details>')


def _automation_panel(state: dict | None, csrf: str, track: str, profile: CandidateProfile | None) -> str:
    if state is None:
        return ""
    connected = state["connected"]
    email = state["email"] or (profile.contact_email if profile else "") or ""
    resume = state["resume_path"]
    if not resume:
        from pathlib import Path
        default = Path(__file__).resolve().parents[4] / "pdf" / "Aliaksandr_Poge_AI_Automation_Resume.pdf"
        if default.is_file():
            resume = str(default)
    flags = "".join(f'<label class="pick"><input type="checkbox" name="{name}" value="yes" '
                    f'{"checked" if state[name] else ""}> {label}</label>' for name, label in (
        ("notifications", "Присылать новые предложения на мою почту"),
        ("mailbox", "Забирать LinkedIn/hh-уведомления из Gmail INBOX"),
        ("send_applications", "Отправлять утверждённые отклики автоматически"),
        ("auto_approve", "Без ручного утверждения: подходящая вакансия + опубликованный адрес для резюме")))
    leads = "".join(f'<article class="discovery-card"><span class="muted">{_escape(item["source"])}</span>'
                    f'<h3>{_escape(item["title"])}</h3>{_vacancy_link(item["url"])}</article>'
                    for item in state.get("leads", []))
    return f'''<section class="search-panel"><h2>Автопилот</h2>
<p class="run-status {'ok' if connected else 'error'}">{'Gmail подключён' if connected else 'Один шаг до отправки: подключи Gmail'}</p>
<div class="metrics"><div class="metric"><strong>{state['sent_total']}</strong><span>Откликов отправлено</span></div>
<div class="metric"><strong>{state['notification_pending']}</strong><span>Новых предложений ждут доставки</span></div>
<div class="metric"><strong>{state['uncertain_total']}</strong><span>Неясных результатов — без повторов</span></div></div>
<p>Поиск → фильтр → сообщение по шаблону → отправка → история. Без AI.</p>
<p class="muted">Автоутверждение: свежие Greenhouse/Ashby вакансии, минимум 2 совпавших навыка, удалёнка Worldwide или Belarus и один явно опубликованный адрес для отклика. Остальные — в очереди ниже.</p>
<details {'open' if not connected else ''}><summary>Почта и настройки отправки</summary>
<form method="post" action="/automation-settings"><input type="hidden" name="csrf" value="{_escape(csrf)}"><input type="hidden" name="track" value="{track}">
<label>Твой Gmail<input type="email" name="email" value="{_escape(email)}" required autocomplete="username"></label>
<label>Пароль приложения Google (не пароль аккаунта)<input type="password" name="password" {'required' if not connected else ''} autocomplete="new-password" placeholder="{'Оставь пустым, чтобы сохранить подключение' if connected else '16 символов'}"></label>
<p class="muted">Нужна двухэтапная проверка. <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener noreferrer">Создать пароль приложения ↗</a>. Вводи его только здесь, не в чате. Доступ проверяется без отправки тестового письма.</p>
<label>Полный путь к резюме PDF<input type="text" name="resume_path" value="{_escape(resume)}"></label>
<label>Максимум откликов за 24 часа<input type="number" name="daily_limit" min="1" max="10" value="{state['daily_limit']}" required></label>
{flags}<p class="muted">Пароль хранится локально в игнорируемом Git файле с доступом только для владельца (0600). Письма читаются без отметки «прочитано». Данные Gmail не отправляются источникам вакансий.</p>
<button type="submit">{'Сохранить и запустить цикл' if connected else 'Подключить Gmail и запустить'}</button></form></details>
<p class="muted">Ссылки из уведомлений не считаются проверенными вакансиями. Чтобы LinkedIn поступал сюда, включи его Job Alerts с доставкой на этот Gmail. Прямые LinkedIn-сообщения и ATS-формы этот канал не отправляет.</p>
{'<h3>Из почтовых уведомлений</h3><div class="discovery-list">' + leads + '</div>' if leads else ''}</section>'''


def _discovered_vacancies(rows: list[dict[str, Any]], profile: CandidateProfile | None) -> tuple[str, int]:
    if profile is None:
        return '<p class="empty">Сначала сохраните профиль кандидата.</p>', 0
    ranked: list[tuple[int, bool, str]] = []
    matching = 0
    for row in rows:
        job = Vacancy(**{field: row[field] for field in Vacancy.__dataclass_fields__ if field in row})
        result = evaluate(profile, job)
        possible = result.status == "review"
        matching += possible
        label = "На проверку" if possible else "Отсеяно"
        reason = result.warnings[0] if possible and result.warnings else result.reasons[0] if result.reasons else ""
        reason = _display_reason(reason)
        title = job.title.casefold()
        relevance = (5 if re.search(r"automat|автомат|workflow|integration|интеграц", title) else 0)
        relevance += 2 if re.search(r"\b(ai|agentic|rpa|n8n)\b", title) else 0
        relevance += sum(1 for skill in profile.skills if skill.casefold() in title)
        if re.search(r"\b(sales|account|marketing|manager|director|qa|quality assurance)\b", title):
            relevance = 0
        relevance += 10 if possible else 0
        ranked.append((relevance, possible, f'''
            <article class="discovery-card">
              <div class="discovery-head"><span class="chip {'possible' if possible else 'filtered'}">{label}</span>
              <span class="muted">{_escape(job.source)}</span></div>
              <h3>{_escape(job.title)}</h3>
              <p class="meta compact" title="{_escape(job.location)}">{_escape(job.company)} · {_escape(job.location)}</p>
              <p class="reason">{_escape(reason)}</p>
              <p>{_vacancy_link(job.url)}</p>
            </article>'''))
    ranked.sort(key=lambda item: (-item[0], not item[1]))
    featured = [item for item in ranked if item[0] > 0][:8]
    cards = "".join(item[2] for item in featured)
    if not cards:
        cards = ('<p class="empty">Среди последних вакансий нет близких к автоматизации. '
                 'Остальные предложения можно открыть ниже.</p>' if ranked else
                 '<p class="empty">Источники ещё не вернули вакансий для этого направления.</p>')
    featured_ids = {id(item) for item in featured}
    remaining = [item[2] for item in ranked if id(item) not in featured_ids]
    rest_html = ""
    if remaining:
        note = '<p class="muted">Показаны первые 50 из остальных.</p>' if len(remaining) > 50 else ""
        rest_html = (f'<details class="all-results"><summary>Остальные найденные вакансии ({len(remaining)})</summary>'
                     f'<div class="discovery-list">{"".join(remaining[:50])}</div>{note}</details>')
    return f'<div class="discovery-list">{cards}</div>{rest_html}', matching


def render_page(rows: list[dict[str, Any]], csrf_token: str, notice: str = "",
                track: str = "belarus", profile: CandidateProfile | None = None,
                discovered: list[dict[str, Any]] | None = None,
                search_status: dict[str, Any] | None = None, automation_state: dict | None = None) -> str:
    if track not in SEARCH_TRACKS:
        raise ValueError("invalid search track")
    track_input = f'<input type="hidden" name="track" value="{track}">'
    cards: list[str] = []
    archived_cards: list[str] = []
    for row in rows:
        review_id = int(row["id"])
        status = row.get("delivery_status") or row["status"]
        warnings = "".join(f"<li>{_escape(_display_reason(item))}</li>" for item in row["warnings"])
        reasons = "".join(f"<li>{_escape(_display_reason(item))}</li>" for item in row["reasons"])
        linkedin_draft = ""
        if profile is not None and row["is_active"]:
            job = Vacancy(source=row.get("source", "manual"), board_token="review", external_id=str(review_id),
                          company=row["company"], title=row["title"], location=row["location"],
                          description=row["description"], url=row["url"], source_updated_at=None,
                          search_track=track)
            result = evaluate(profile, job)
            if result.status == "review":
                message = render_linkedin_message(profile, job, result)
                linkedin_draft = (f'<details><summary>Сообщение рекрутеру для LinkedIn · не отправлено</summary>'
                                  f'<p class="warning">Текст подготовлен автоматически. Проверь адресата и скопируй его вручную в LinkedIn.</p>'
                                  f'<textarea readonly aria-label="Сообщение рекрутеру">{_escape(message)}</textarea></details>')
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
        if row.get("source") == "remotive":
            source_note = '<p class="warning">Источник: <a href="https://remotive.com/" target="_blank" rel="noopener noreferrer">Remotive</a>. Условия проверяй у работодателя.</p>'
        target_cards = archived_cards if row["status"] == "rejected" else cards
        target_cards.append(f'''
            <article class="card">
                <div class="top"><span class="status {status}">{_escape(_STATUS_LABELS[status])}</span><span class="score">Оценка {int(row['score'])}/100</span></div>
                <h2>{_escape(row['title'])}</h2>
                <p class="meta">{_escape(row['company'])} · {_escape(row['location'])}</p>
                <p>{_vacancy_link(str(row['url']))}</p>
                {source_note}
                {stale}
                <details><summary>Почему подобрана · условия проверки</summary><div class="detail-grid"><div><h3>Совпадения</h3><ul>{reasons}</ul></div><div><h3>Проверить вручную</h3><ul>{warnings}</ul></div></div></details>
                <details><summary>Описание вакансии</summary><p class="description">{_escape(row['description'])}</p></details>
                {linkedin_draft}
                {controls}
            </article>''')
    cards_html = "".join(cards) if cards else '<p class="empty">Подходящих черновиков пока нет. Ниже видны найденные вакансии и причины отсева.</p>'
    archive_html = (f'<details class="all-results"><summary>Отклонённые черновики ({len(archived_cards)})</summary>'
                    f'{"".join(archived_cards)}</details>') if archived_cards else ""
    notice_html = f'<p class="notice" role="status">{_escape(notice)}</p>' if notice else ""
    draft_ids = sum(row["status"] == "draft" and row["is_active"] for row in rows)
    discovered = discovered or []
    discovery_html, possible_count = _discovered_vacancies(discovered, profile)
    search_button = (f'<form method="post" action="/search-now"><input type="hidden" name="csrf" value="{_escape(csrf_token)}">'
                     f'{track_input}<button type="submit">Искать сейчас</button></form>') if search_status is not None else ""
    return f'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Поиск работы · Automation Engineer</title><style>
:root {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #17253d; background: #eef3f9; }}
body {{ margin: 0; }} main {{ max-width: 1120px; margin: auto; padding: 32px 18px 70px; }}
h1 {{ margin: 0 0 8px; font-size: 2.2rem; }} h2 {{ margin: 12px 0 4px; }} h3 {{ font-size: .92rem; margin: 0 0 8px; }}
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
input[type=password],input[type=text],input[type=number] {{ box-sizing:border-box; width:100%; padding:10px; font:inherit; border:1px solid #b9c8dc; border-radius:8px; margin:8px 0; }}
.pick {{ display:block; margin:14px 0; font-weight:600; }} .reject {{ display:inline-block; margin-left:8px; }} .reject button {{ background:#f1e9e9; color:#7f2929; }}
.saved-draft pre {{ white-space:pre-wrap; font:inherit; }} .warning {{ color:#9a4a05; }} .notice {{ background:#e2f4e7; padding:12px; border-radius:8px; }}
.empty {{ padding:28px; background:white; border-radius:12px; }} .safety {{ background:#fff4dc; padding:12px; border-radius:8px; }}
.tabs {{ display:flex; gap:8px; margin:20px 0; flex-wrap:wrap; }} .tabs a {{ padding:10px 15px; border-radius:8px; color:#144783; background:#e7eef9; text-decoration:none; font-weight:600; }}
.tabs a[aria-current="page"] {{ color:white; background:#1157ad; }}
.hero {{ background:linear-gradient(125deg,#102f59,#175e9f); color:white; border-radius:20px; padding:30px; box-shadow:0 14px 30px #10376426; }}
.hero .subtitle {{ color:#d9eaff; }} .hero h1 {{ letter-spacing:-.03em; }}
.metrics {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin:18px 0; }}
.metric {{ background:white; border:1px solid #dce5f1; border-radius:14px; padding:18px 20px; }}
.metric strong {{ display:block; font-size:1.9rem; color:#164d89; }} .metric span {{ color:#5d6a7d; font-size:.9rem; }}
.search-panel {{ background:white; border:1px solid #dce5f1; border-radius:14px; padding:20px; margin:18px 0; }}
.search-panel h2 {{ margin-top:0; }} .run-status {{ margin-bottom:4px; font-weight:600; }}
.run-status.error {{ color:#9a4a05; }} .run-status.ok {{ color:#126447; }} .run-status.running {{ color:#1557a0; }}
.muted {{ color:#68778c; font-size:.9rem; }} .section-title {{ margin:32px 0 10px; }}
.discovery-list {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; margin-top:14px; }}
.discovery-card {{ background:white; border:1px solid #dce5f1; border-radius:12px; padding:17px; }}
.discovery-card h3 {{ font-size:1rem; margin:10px 0 4px; }} .discovery-card p {{ margin:7px 0; }}
.discovery-head {{ display:flex; align-items:center; justify-content:space-between; }}
.chip {{ display:inline-block; border-radius:99px; padding:4px 9px; font-size:.77rem; font-weight:700; }}
.chip.possible {{ background:#ddf3e5; color:#176845; }} .chip.filtered {{ background:#eef1f5; color:#647084; }}
.reason {{ color:#34465b; font-size:.88rem; }}
.compact {{ display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }}
.all-results {{ background:#fff; border:1px solid #dce5f1; border-radius:12px; padding:14px 18px; margin:18px 0; }}
.all-results summary {{ color:#164d89; }}
@media(max-width:680px) {{ .detail-grid,.discovery-list,.metrics {{ grid-template-columns:1fr; }} .hero {{ padding:22px; }} }}
</style></head><body><main>
<div class="hero"><h1>Поиск работы</h1><p class="subtitle">Automation Engineer · удалённо · Беларусь и международный рынок</p></div>
<nav class="tabs" aria-label="Направление поиска">
<a href="/?track=belarus" {'aria-current="page"' if track == 'belarus' else ''}>В Беларуси</a>
<a href="/?track=international" {'aria-current="page"' if track == 'international' else ''}>За рубежом · удалённо</a>
</nav>
<p class="subtitle">{'Вакансии, которые источник относит к удалённой работе из Беларуси. Условия подтверждайте у работодателя.' if track == 'belarus' else 'Международный поиск. Возможность работать из Беларуси проверяйте у работодателя; страна компании не подтверждена автоматически.'}</p>
{notice_html}
<div class="metrics"><div class="metric"><strong>{len(discovered)}</strong><span>Проверено последних вакансий (до 200)</span></div>
<div class="metric"><strong>{possible_count}</strong><span>Прошли первичный фильтр</span></div>
<div class="metric"><strong>{draft_ids}</strong><span>Черновиков на проверку</span></div></div>
<section class="search-panel"><h2>Автоматический поиск</h2>{_search_status(search_status)}{search_button}
<p class="muted">Greenhouse · Ashby · Himalayas · Remotive · hh. Ошибка одного источника не останавливает остальные.</p></section>
{_automation_panel(automation_state, csrf_token, track, profile)}
<h2 class="section-title">Очередь откликов</h2>
<p class="safety">Утверждённый отклик с адресом работодателя будет отправлен следующим циклом, если Gmail подключён и отправка включена.</p>
<div class="toolbar">
<form method="post" action="/refresh" class="secondary"><input type="hidden" name="csrf" value="{_escape(csrf_token)}">{track_input}<button type="submit">Обновить очередь</button></form>
<form method="post" action="/approve" id="approve-form"><input type="hidden" name="csrf" value="{_escape(csrf_token)}">{track_input}<label class="pick"><input type="checkbox" name="checked" value="yes" required> Я проверил условия вакансий</label><button type="submit">Утвердить выбранные ({draft_ids} доступны)</button></form>
</div>{cards_html}{archive_html}
<h2 class="section-title">Что нашлось в источниках</h2>
<p class="muted">Сначала близкие по теме, остальные — внутри списка. Это сохранённые вакансии; не все источники обновлялись последним запуском. «На проверку» не подтверждает право работать из Беларуси.</p>
{discovery_html}
</main></body></html>'''


def _ids(values: list[str]) -> list[int]:
    if not values or len(values) > 100 or any(not re.fullmatch(r"[1-9]\d*", value) for value in values):
        raise ValueError("select 1 to 100 draft items")
    result = [int(value) for value in values]
    if len(set(result)) != len(result):
        raise ValueError("duplicate draft ids are not allowed")
    return result


def make_handler(repository: Repository, csrf_token: str, search_runner: SearchRunner | None = None,
                 automation: AutomationPipeline | None = None):
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
                profile = repository.get_profile()
                self._respond(200, render_page(repository.list_reviews(track=track), csrf_token, notice, track,
                                               profile, repository.list_vacancies(200, track=track),
                                               search_runner.snapshot() if search_runner else None,
                                               automation.snapshot() if automation else None))
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
                if path == "/automation-settings":
                    if automation is None:
                        raise ValueError("automation is not configured")
                    previous = automation.store.load()
                    email = data.get("email", [""])[0].strip()
                    password = "".join(data.get("password", [""])[0].split())
                    if not password and email == previous.email:
                        password = previous.password
                    settings = AutomationSettings.from_dict({
                        "email": email, "password": password, "resume_path": data.get("resume_path", [""])[0].strip(),
                        "daily_limit": int(data.get("daily_limit", ["5"])[0]),
                        **{name: data.get(name) == ["yes"] for name in ("notifications", "mailbox", "send_applications", "auto_approve")}})
                    if not settings.connected:
                        raise ValueError("enter Gmail and an app password")
                    settings.smtp(require_resume=settings.send_applications)
                    if settings.email != previous.email or settings.password != previous.password:
                        try:
                            verify_gmail(settings.email, settings.password)
                        except Exception:
                            raise ValueError("Gmail connection failed. Check the app password and account access.") from None
                    automation.store.save(settings)
                    started = search_runner.trigger() if search_runner else False
                    notice = "Настройки сохранены. Цикл запущен." if started else "Настройки сохранены; применятся в следующем цикле."
                elif path == "/refresh":
                    result = refresh_queue(repository, track=track)
                    notice = f"Найдено подходящих: {result['matching']}; новых черновиков: {result['added']}."
                elif path == "/search-now":
                    if search_runner is None:
                        raise ValueError("automatic search is not configured")
                    notice = ("Поиск запущен. Обнови страницу, чтобы увидеть результат."
                              if search_runner.trigger() else "Поиск уже выполняется.")
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


def serve(repository: Repository, port: int = 8765, *, search_runner: SearchRunner | None = None,
          automation: AutomationPipeline | None = None) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    server = ThreadingHTTPServer(("127.0.0.1", port),
                                 make_handler(repository, secrets.token_urlsafe(32), search_runner, automation))
    print(f"Review queue: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        if search_runner is not None:
            search_runner.start()
        server.serve_forever()
    finally:
        if search_runner is not None:
            search_runner.stop()
        server.server_close()
