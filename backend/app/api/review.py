import html
import re
import secrets
from pathlib import Path
from dataclasses import replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from app.applications.queue import refresh_queue
from app.api.search_runner import SearchRunner
from app.applications.automation import AutomationPipeline, AutomationSettings
from app.integrations.mailbox import verify_gmail
from app.integrations.gmail_api import GmailAPIError
from app.candidate.profile import CandidateProfile
from app.infrastructure.persistence.postgres import Repository
from app.matching.rules import evaluate
from app.matching.screening import screen
from app.templates.drafts import render_linkedin_message
from app.vacancies.models import SEARCH_TRACKS
from app.vacancies.models import Vacancy

_MAX_BODY_BYTES = 64_000
_DASHBOARD_CSS = Path(__file__).with_name("dashboard.css").read_text(encoding="utf-8")
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
        "Senior/lead role is outside the automatic search focus.": "Senior/Lead вне текущего автоматического поиска.",
        "Full description is missing or too short to verify responsibilities.": "Нет полного описания для проверки обязанностей.",
        "Business/workflow automation responsibilities are not confirmed.": "Автоматизация бизнес-процессов в обязанностях не подтверждена.",
        "Description concerns test or industrial automation, not business workflows.": "В описании — тестовая или промышленная автоматизация, не бизнес-процессы.",
        "Fewer than two profile skills are confirmed in the description.": "Подтверждено меньше двух навыков из профиля.",
        "Fully remote work needs confirmation; office/hybrid or description-only evidence.": "Нужно подтвердить полностью удалённый формат: есть офис/гибрид или только упоминание в описании.",
        "Country of residence is not configured.": "Страна проживания не настроена.",
        "EU/EEA residence or citizenship is required; Belarus does not satisfy that scope.": "Требуется проживание/гражданство ЕС или ЕЭЗ; Беларусь не входит в этот регион.",
        "Description restricts hiring to other countries.": "В описании найм ограничен другими странами.",
        "Residence/work-authorization restrictions need human confirmation.": "Ограничения по проживанию или разрешению на работу требуют проверки.",
        "Remote Europe/EMEA or a listing region does not confirm hiring from your residence.": "Europe/EMEA или регион публикации не подтверждает возможность работать из Беларуси.",
        "Minimum salary has not been verified.": "Минимальная зарплата не проверена.",
        "Required years of experience are not verified against the candidate profile.": "Требуемый стаж нужно сверить с фактическим опытом кандидата.",
        "Role, workflow responsibilities, skills and remote residence scope match the published text.": "По тексту совпали роль, автоматизация процессов, навыки и география удалённой работы.",
        "Only an alert link is available; full description and hiring scope are not verified.": "Есть только ссылка: описание и география найма не проверены.",
        "hh description unavailable; bounded API limit/cooldown, retry later.": "Описание hh пока недоступно: лимит или пауза между API-запросами.",
        "hh vacancy is archived or not fully remote.": "Вакансия hh в архиве или не полностью удалённая.",
        "Full description loaded from the official hh API.": "Полное описание загружено через официальный API hh.",
    }
    if reason.startswith("Listed remote locations do not include"):
        return "Указанные страны удалённой работы не включают страну проживания."
    if reason.startswith("The vacancy explicitly excludes applicants from"):
        return "Вакансия исключает кандидатов из страны проживания."
    if reason.startswith("Unverified alert title: "):
        return "По заголовку письма: " + _display_reason(reason.removeprefix("Unverified alert title: "))
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
                   f"ошибок: {report['failed']} · новых записей: {new_count} · отправлено откликов: {report.get('sent', 0)}.")
        style = "ok" if not report["failed"] else "error"
    else:
        summary = "Первый поиск запустится при открытии сервера."
        style = "running"
    finished = search_status["finished_at"]
    when = datetime.fromisoformat(finished).astimezone().strftime("%d.%m.%Y %H:%M") if finished else "ещё не завершён"
    hours = search_status["interval_seconds"] // 3600
    interval = f"каждые {hours} ч" if hours else f"каждые {search_status['interval_seconds'] // 60} мин"
    report = search_status.get("report") or {}
    source_details = ""
    for item in report.get("sources", []):
        kind, _, target = item["source"].partition(":")
        label = {"ashby": "Ashby", "greenhouse": "Greenhouse", "himalayas": "Himalayas",
                 "remotive": "Remotive", "hh": "hh.ru"}.get(kind, kind)
        if kind in {"ashby", "greenhouse"}:
            label = {"n8n": "n8n", "zapier": "Zapier", "workato": "Workato"}.get(target, target) + " · " + label
        elif kind == "himalayas":
            label += " · " + target.removeprefix("BY:").removesuffix(":worldwide")
        state = item["status"]
        badge = {"ok": "Доступен", "failed": "Проблема доступа", "cooldown": "Пауза по лимиту"}.get(state, "Ожидает")
        note = (f"Получено описаний: {item.get('total', 0)} · новых: {item.get('new', 0)}" if state == "ok" else
                "Следующее обновление — после паузы в 6 часов" if state == "cooldown" else
                "Источник вернул 403. Остальные продолжают работу." if "403" in item.get("error", "") else
                "Не удалось загрузить данные. Остальные источники работают.")
        source_details += (f'<article class="source-card"><div class="source-top"><span class="source-name">{_escape(label)}</span>'
                           f'<span class="source-status {_escape(state)}">{badge}</span></div><p>{_escape(note)}</p></article>')
    stages = report.get("automation", {})
    stage_details = ""
    for key, label in (("mailbox", "Почтовые уведомления"), ("alert_screening", "Ссылки из писем"),
                       ("screening", "Проверка описаний"), ("applications", "Отклики работодателям"),
                       ("digest", "Подборка на твою почту")):
        value = stages.get(key)
        if value is None:
            continue
        if isinstance(value, dict):
            fields = {"processed": "писем обработано", "new_links": "новых ссылок", "matched": "подходит",
                      "manual": "на проверку", "rejected": "отсеяно", "sent": "отправлено",
                      "items": "вакансий в подборке", "uncertain": "результат неясен"}
            note = " · ".join(f"{label}: {value[name]}" for name, label in fields.items() if name in value)
            if "detail_error" in value:
                note += " · Описания hh недоступны; ссылки сохранены для проверки."
        else:
            note = {"disabled": "Этап выключен в настройках", "not connected": "Нужно подключить Gmail"}.get(value,
                    "Не завершено. Проверь подключение и детали ниже.")
        stage_details += f'<div class="stage-row"><strong>{label}</strong><span>{_escape(note)}</span></div>'
    return (f'<p class="run-status {style}" role="status">{summary}</p>'
            f'<p class="muted">Последняя проверка: {_escape(when)} · Повтор: {interval}, пока приложение запущено.</p>'
            f'<div class="source-grid">{source_details}</div>'
            f'<details><summary>Источники и результаты этапов</summary><div class="stage-list">{stage_details}</div></details>')


def _overview(state: dict | None, search_status: dict | None, track: str) -> str:
    state = state or {}
    counts = state.get("screening_counts", {})
    connected = bool(state.get("connected"))
    sends = connected and bool(state.get("send_applications"))
    notices = connected and bool(state.get("notifications"))
    configured = search_status is not None
    limit = state.get("daily_limit", 5)
    attempts = state.get("attempts_24h", 0)
    matched, manual = counts.get("matched", 0), counts.get("manual", 0)
    if state.get("uncertain_total", 0):
        heading, message = "Проверь отправленные письма", "Есть неясный результат отправки. Повтор заблокирован, чтобы не отправить письмо дважды."
    elif not connected:
        heading, message = "Подключи Gmail для отправки", "Вакансии можно искать без почты. Подключение находится в настройках ниже."
    elif not matched and manual:
        heading, message = "Есть вакансии на проверку", "Проверь ограничения по стране и требованиям в очереди. Без подтверждения автоматический отклик не уйдёт."
    elif not matched:
        heading, message = "Ждём подходящую вакансию", "Новых подтверждённых предложений нет. Поиск продолжится по расписанию; неподходящие вакансии не отправляются."
    elif not sends:
        heading, message = "Отправка откликов выключена", "Подходящие вакансии найдены. Проверь резюме и настройки отправки."
    else:
        heading, message = "Подходящие вакансии найдены", "Подборка поступит на почту, если включена. Автоотклик возможен только на опубликованный адрес и в пределах лимита."
    scope = "В Беларуси" if track == "belarus" else "За рубежом · удалённо"
    return f'''<div class="metrics" aria-label="Сводка выбранного направления">
<div class="metric featured"><span class="metric-label">Подходит по тексту</span><strong>{matched}</strong><span>{scope}</span></div>
<div class="metric"><span class="metric-label">Нужна проверка</span><strong>{manual}</strong><span>Условия ещё не подтверждены</span></div>
<div class="metric"><span class="metric-label">Откликов отправлено</span><strong>{state.get('sent_total', 0)}</strong><span>Всего · оба направления</span></div>
<div class="metric"><span class="metric-label">Попытки за 24 часа</span><strong>{attempts} <em>/ {limit}</em></strong><span>Общий лимит, включая неясные</span></div></div>
<div class="overview-grid"><section class="search-panel"><div class="section-heading"><h2>Как работает автопилот</h2><span class="muted">Без AI</span></div>
<div class="pipeline"><div class="pipeline-step"><span class="step-number">01</span><div><strong>Собирает вакансии</strong><p>Доски работодателей, агрегаторы и письма</p></div><span class="step-state {'off' if not configured else ''}">{'По расписанию' if configured else 'Не настроено'}</span></div>
<div class="pipeline-step"><span class="step-number">02</span><div><strong>Проверяет описание</strong><p>Роль, навыки, удалёнка и ограничения по стране</p></div><span class="step-state">Строгий фильтр</span></div>
<div class="pipeline-step"><span class="step-number">03</span><div><strong>Отправляет по правилам</strong><p>Подборки: {'включены' if notices else 'выключены'} · отклики: {'включены' if sends else 'выключены'}</p></div><span class="step-state {'off' if not sends else ''}">{'До ' + str(limit) + ' в сутки' if sends else 'Отправка выкл.'}</span></div></div>
<p class="muted">Для работы нужны запущенное приложение, база данных и компьютер без режима сна. LinkedIn-сообщения и формы откликов пока не отправляются.</p></section>
<section class="search-panel"><div class="section-heading"><h2>Что требует внимания</h2></div><div class="next-action"><strong>{heading}</strong><p>{message}</p></div>
<p class="muted">Gmail: {'подключён' if connected else 'не подключён'} · автоутверждение: {'включено' if state.get('auto_approve') else 'выключено'}.</p>
<div class="button-row"><a class="button" href="#queue">Открыть очередь ↗</a><a href="#settings" class="muted">Настройки</a></div></section></div>'''


def _automation_panel(state: dict | None, csrf: str, track: str, profile: CandidateProfile | None) -> str:
    if state is None:
        return ""
    connected = state["connected"]
    oauth = state.get("oauth", {"connected": False, "client_configured": False})
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
    lead_labels = {"pending": "Ожидает проверки", "manual": "Нужна проверка", "rejected": "Отсеяно по письму", "resolved": "Описание получено"}
    leads = "".join(f'<article class="discovery-card"><span class="muted">{_escape(item["source"])} · '
                    f'{lead_labels.get(item.get("screening_status", "pending"), "Нужна проверка")}</span>'
                    f'<h3>{_escape(item["title"])}</h3><p class="reason">{_escape(_display_reason(item.get("screening_reason", "")))}</p>'
                    f'{_vacancy_link(item["url"])}</article>'
                    for item in state.get("leads", []))
    screening = state.get("screening_counts", {})
    alert_stats = state.get("alert_screening", {})
    alert_summary = " · ".join(f"{lead_labels.get(key, key)}: {value}" for key, value in alert_stats.items())
    return f'''<section class="search-panel"><h2>Автопилот</h2>
<p class="run-status {'ok' if connected else 'error'}">{'Gmail API подключён · HTTPS' if oauth['connected'] else 'Gmail подключён · старый SMTP-канал' if connected else 'Поиск работает · почта пока не подключена'}</p>
<div class="metrics"><div class="metric"><strong>{state['sent_total']}</strong><span>Откликов отправлено</span></div>
<div class="metric"><strong>{state['notification_pending']}</strong><span>Новых предложений ждут доставки</span></div>
<div class="metric"><strong>{state['uncertain_total']}</strong><span>Неясных результатов — без повторов</span></div></div>
<h3>Проверка описаний</h3>
<p class="run-status ok">Подходит по тексту: {screening.get('matched', 0)} · Нужна проверка: {screening.get('manual', 0)} · Отсеяно: {screening.get('rejected', 0)}</p>
<p class="muted">Подборки содержат только проверенные описания: инженерная автоматизация бизнес-процессов, минимум два навыка, полностью удалённо и явная география Belarus/Worldwide. Europe/EMEA, Senior/Lead, офис/гибрид и неполные данные не проходят автоматический отбор. Совпадение по тексту не гарантирует юридическую возможность найма.</p>
<p class="muted">Для hh и Himalayas регион публикации/классификация Worldwide не считается подтверждением: нужна явная география в самом описании. Требования к стажу и разрешению на работу проверяются вручную.</p>
<p class="muted">Старых уведомлений удержано от отправки: {state.get('notification_held', 0)}. История не удалена; непроверенные ссылки не отправляются. {_escape(alert_summary)}</p>
<p>Поиск → фильтр → сообщение по шаблону → отправка → история. Без AI.</p>
<p class="muted">Автоутверждение: свежие Greenhouse/Ashby вакансии, минимум 2 совпавших навыка, удалёнка Worldwide или Belarus и один явно опубликованный адрес для отклика. Остальные — в очереди ниже.</p>
<h3>Gmail без пароля приложения</h3>
<p>OAuth через HTTPS, без SMTP. Разрешения: чтение почты и отправка писем. Google даёт чтение всей почты; приложение обрабатывает только LinkedIn/hh-уведомления в INBOX, не меняет метки и не удаляет письма.</p>
<p class="muted">OAuth-клиент: {'настроен' if oauth['client_configured'] else 'не настроен'}. Один раз в Google Cloud: включи Gmail API, настрой Google Auth Platform (External → Testing), добавь свой Gmail как test user, создай клиент типа Desktop app и скачай JSON. <a href="https://console.cloud.google.com/apis/library/gmail.googleapis.com" target="_blank" rel="noopener noreferrer">Google Cloud ↗</a>. В режиме Testing доступ обычно истекает через 7 дней; потребуется повторный вход.</p>
<details><summary>Настроить OAuth-клиент · JSON из Google Cloud</summary>
<form method="post" action="/gmail-client"><input type="hidden" name="csrf" value="{_escape(csrf)}"><input type="hidden" name="track" value="{track}">
<label>JSON OAuth-клиента Desktop app<textarea name="client_json" rows="5" required autocomplete="off" placeholder='Вставь содержимое скачанного JSON здесь, не в чате'></textarea></label>
<button type="submit">Сохранить OAuth-клиент локально</button></form></details>
<form method="post" action="/gmail-connect"><input type="hidden" name="csrf" value="{_escape(csrf)}"><input type="hidden" name="track" value="{track}">
<button type="submit" {'disabled' if not oauth['client_configured'] else ''}>Войти с Google · системный браузер</button></form>
{'<form method="post" action="/gmail-disconnect"><input type="hidden" name="csrf" value="' + _escape(csrf) + '"><input type="hidden" name="track" value="' + track + '"><button type="submit">Отключить почту локально</button></form>' if connected else ''}
<details {'open' if not connected else ''}><summary>Почта и настройки отправки</summary>
<form method="post" action="/automation-settings"><input type="hidden" name="csrf" value="{_escape(csrf)}"><input type="hidden" name="track" value="{track}">
<label>Твой Gmail<input type="email" name="email" value="{_escape(email)}" readonly autocomplete="off"></label>
<p class="muted">Адрес подтверждается Google при входе. Настройки можно сохранить без подключения — поиск продолжится, письма не уйдут.</p>
<label>Полный путь к резюме PDF<input type="text" name="resume_path" value="{_escape(resume)}"></label>
<label>Максимум откликов за 24 часа<input type="number" name="daily_limit" min="1" max="10" value="{state['daily_limit']}" required></label>
{flags}<p class="muted">OAuth-токены и настройки хранятся локально вне Git с правами 0600, без шифрования. Пароль Google приложению не нужен. После подключения и сохранения настроек включённые этапы могут отправлять письма.</p>
<button type="submit">{'Сохранить и запустить цикл' if connected else 'Сохранить настройки · продолжить без почты'}</button></form></details>
<p class="muted">Ссылки из уведомлений не считаются проверенными вакансиями. Чтобы LinkedIn поступал сюда, включи его Job Alerts с доставкой на этот Gmail. Прямые LinkedIn-сообщения и ATS-формы этот канал не отправляет.</p>
{'<details><summary>Ссылки из почтовых уведомлений · не проверенные вакансии</summary><div class="discovery-list">' + leads + '</div></details>' if leads else ''}</section>'''


def _discovered_vacancies(rows: list[dict[str, Any]], profile: CandidateProfile | None) -> tuple[str, int]:
    if profile is None:
        return '<p class="empty">Сначала сохраните профиль кандидата.</p>', 0
    ranked: list[tuple[int, str, str]] = []
    matching = 0
    seen = set()
    for row in rows:
        job = Vacancy(**{field: row[field] for field in Vacancy.__dataclass_fields__ if field in row})
        result = screen(profile, job)
        identity = (job.source, job.external_id)
        if identity in seen:
            continue
        seen.add(identity)
        possible = result.status == "matched"
        matching += possible
        label = {"matched": "Подходит по тексту", "manual": "Нужна проверка", "rejected": "Отсеяно"}[result.status]
        reason = _display_reason(result.reason)
        title = job.title.casefold()
        relevance = (5 if re.search(r"automat|автомат|workflow|integration|интеграц", title) else 0)
        relevance += 2 if re.search(r"\b(ai|agentic|rpa|n8n)\b", title) else 0
        relevance += sum(1 for skill in profile.skills if skill.casefold() in title)
        if re.search(r"\b(sales|account|marketing|manager|director|qa|quality assurance)\b", title):
            relevance = 0
        relevance += 10 if possible else 0
        ranked.append((relevance, result.status, f'''
            <article class="discovery-card">
              <div class="discovery-head"><span class="chip { {'matched': 'possible', 'manual': 'manual', 'rejected': 'filtered'}[result.status] }">{label}</span>
              <span class="muted">{_escape(job.source)}</span></div>
              <h3>{_escape(job.title)}</h3>
              <p class="meta compact" title="{_escape(job.location)}">{_escape(job.company)} · {_escape(job.location)}</p>
              <p class="reason">{_escape(reason)}</p>
              <p>{_vacancy_link(job.url)}</p>
            </article>'''))
    ranked.sort(key=lambda item: -item[0])
    matched = [item[2] for item in ranked if item[1] == "matched"]
    manual = [item[2] for item in ranked if item[1] == "manual"]
    rejected = [item[2] for item in ranked if item[1] == "rejected"]
    cards = ('<div class="discovery-list">' + "".join(matched[:12]) + '</div>' if matched else
             '<div class="empty"><strong>Подтверждённых предложений пока нет</strong>'
             'Это не ошибка отправки. Сейчас нет вакансий, прошедших все проверки. '
             'Автопилот продолжит поиск по расписанию.</div>')
    if len(matched) > 12:
        cards += '<p class="muted">Показаны первые 12 подходящих вакансий из последних загруженных.</p>'
    if manual:
        cards += (f'<h3 class="group-title">На проверку перед откликом <span class="count-badge">{len(manual)}</span></h3>'
                  f'<div class="discovery-list">{"".join(manual[:10])}</div>')
        if len(manual) > 10:
            cards += '<p class="muted">Показаны первые 10 вакансий на проверку.</p>'
    if rejected:
        cards += (f'<details class="all-results"><summary>Отсеянные вакансии ({len(rejected)}) · показать причины</summary>'
                  f'<div class="discovery-list">{"".join(rejected[:50])}</div>'
                  '<p class="muted">Показаны до 50 отсеянных вакансий. Они не отправляются автоматически.</p></details>')
    return cards, matching


def render_page(rows: list[dict[str, Any]], csrf_token: str, notice: str = "",
                track: str = "belarus", profile: CandidateProfile | None = None,
                discovered: list[dict[str, Any]] | None = None,
                search_status: dict[str, Any] | None = None, automation_state: dict | None = None) -> str:
    if track not in SEARCH_TRACKS:
        raise ValueError("invalid search track")
    track_input = f'<input type="hidden" name="track" value="{track}">'
    cards: list[str] = []
    archived_cards: list[str] = []
    duplicate_cards: list[str] = []
    seen_drafts: set[tuple] = set()
    for row in rows:
        review_id = int(row["id"])
        status = row.get("delivery_status") or row["status"]
        warnings = "".join(f"<li>{_escape(_display_reason(item))}</li>" for item in row["warnings"])
        reasons = "".join(f"<li>{_escape(_display_reason(item))}</li>" for item in row["reasons"])
        linkedin_draft = ""
        screening_note = ""
        if profile is not None and row["is_active"]:
            job = Vacancy(source=row.get("source", "manual"), board_token="review", external_id=str(review_id),
                          company=row["company"], title=row["title"], location=row["location"],
                          description=row["description"], url=row["url"], source_updated_at=None,
                          search_track=track)
            result = evaluate(profile, job)
            screened = screen(profile, job)
            style = "run-status ok" if screened.status == "matched" else "warning"
            screening_note = f'<p class="{style}">Автофильтр: {_escape(_display_reason(screened.reason))}</p>'
            if result.status == "review":
                message = render_linkedin_message(profile, job, result)
                linkedin_draft = (f'<details><summary>Сообщение рекрутеру для LinkedIn · не отправлено</summary>'
                                  f'<p class="warning">Текст подготовлен автоматически. Проверь адресата и скопируй его вручную в LinkedIn.</p>'
                                  f'<textarea readonly aria-label="Сообщение рекрутеру">{_escape(message)}</textarea></details>')
        controls = ""
        if status == "draft" and row["is_active"]:
            controls = f'''
                <label class="pick"><input type="checkbox" name="id" value="{review_id}" form="approve-form"> Выбрать для утверждения</label>
                <details><summary>Текст отклика · посмотреть и изменить</summary>
                <form method="post" action="/edit/{review_id}">
                    <input type="hidden" name="csrf" value="{_escape(csrf_token)}">
                    {track_input}
                    <label for="draft-{review_id}">Текст отклика</label>
                    <textarea id="draft-{review_id}" name="draft" maxlength="10000" required>{_escape(row['draft_text'])}</textarea>
                    <button type="submit">Сохранить текст</button>
                </form></details>
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
        if status == "draft" and row.get("source") and row.get("external_id"):
            key = (row["source"], row["external_id"], row["draft_text"], row["is_active"])
            if key in seen_drafts:
                target_cards = duplicate_cards
            seen_drafts.add(key)
        target_cards.append(f'''
            <article class="card">
                <div class="top"><span class="status {status}">{_escape(_STATUS_LABELS[status])}</span><span class="score">Оценка {int(row['score'])}/100</span></div>
                <h2>{_escape(row['title'])}</h2>
                <p class="meta">{_escape(row['company'])} · {_escape(row['location'])}</p>
                <p>{_vacancy_link(str(row['url']))}</p>
                {source_note}
                {screening_note}
                {stale}
                <details><summary>Почему подобрана · условия проверки</summary><div class="detail-grid"><div><h3>Совпадения</h3><ul>{reasons}</ul></div><div><h3>Проверить вручную</h3><ul>{warnings}</ul></div></div></details>
                <details><summary>Описание вакансии</summary><p class="description">{_escape(row['description'])}</p></details>
                {linkedin_draft}
                {controls}
            </article>''')
    cards_html = "".join(cards) if cards else '<p class="empty">Подходящих черновиков пока нет. Ниже видны найденные вакансии и причины отсева.</p>'
    if duplicate_cards:
        cards_html += (f'<details class="all-results"><summary>Повторные черновики ({len(duplicate_cards)}) · из других поисковых запросов</summary>'
                       '<p class="muted">Одинаковая вакансия встретилась в нескольких запросах. Все тексты сохранены; достаточно утвердить один отклик.</p>'
                       + "".join(duplicate_cards) + '</details>')
    archive_html = (f'<details class="all-results"><summary>Отклонённые черновики ({len(archived_cards)})</summary>'
                    f'{"".join(archived_cards)}</details>') if archived_cards else ""
    notice_html = f'<p class="notice" role="status">{_escape(notice)}</p>' if notice else ""
    draft_ids = len({(row.get("source") or "review", row.get("external_id") or row["id"])
                     for row in rows if row["status"] == "draft" and row["is_active"]})
    discovered = discovered or []
    discovery_html, possible_count = _discovered_vacancies(discovered, profile)
    search_button = (f'<form method="post" action="/search-now"><input type="hidden" name="csrf" value="{_escape(csrf_token)}">'
                     f'{track_input}<button type="submit" {"disabled" if search_status["running"] else ""}>Искать сейчас</button></form>') if search_status is not None else ""
    candidate_name = profile.full_name if profile else "Мой профиль"
    ready = bool(automation_state and automation_state.get("connected"))
    scope = "Беларусь" if track == "belarus" else "Международный поиск"
    return f'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Поиск работы · Automation Engineer</title><style>{_DASHBOARD_CSS}</style></head><body>
<div class="app-layout"><aside class="sidebar"><a href="#overview" class="brand"><span class="brand-mark">↗</span> flow / jobs</a>
<p class="sidebar-caption">Рабочая панель</p><nav class="side-nav" aria-label="Разделы панели">
<a class="active" href="#overview"><span class="nav-symbol">◈</span>Обзор</a>
<a href="#opportunities"><span class="nav-symbol">▤</span>Вакансии</a>
<a href="#queue"><span class="nav-symbol">↗</span>Очередь откликов</a>
<a href="#sources"><span class="nav-symbol">◎</span>Источники</a>
<a href="#settings"><span class="nav-symbol">⚙</span>Настройки</a></nav>
<div class="sidebar-footer"><strong>Automation Engineer</strong>Удалённо из Беларуси<br>Поиск и отклики по правилам<br>Без встроенного AI</div></aside>
<main id="overview"><div class="topbar"><span>МОЯ РАБОТА / ОБЗОР</span><span class="account">{_escape(candidate_name)}</span></div>
<div class="hero"><div><p class="eyebrow">Твой поиск, под контролем</p><h1>Поиск работы</h1><p class="subtitle">Automation Engineer · удалённо · Беларусь и международный рынок</p></div>
<div class="hero-note"><span class="pill"><span class="dot"></span>{'Gmail подключён' if ready else 'Поиск без почты'}</span><p>Письма только по правилам</p></div></div>
<nav class="tabs" aria-label="Направление поиска">
<a href="/?track=belarus" {'aria-current="page"' if track == 'belarus' else ''}>В Беларуси</a>
<a href="/?track=international" {'aria-current="page"' if track == 'international' else ''}>За рубежом · удалённо</a>
</nav>
<p class="scope-note">{'Вакансии, которые источник относит к удалённой работе из Беларуси. Условия подтверждайте у работодателя.' if track == 'belarus' else 'Международный поиск. Возможность работать из Беларуси проверяйте у работодателя; страна компании не подтверждена автоматически.'}</p>
{notice_html}
{_overview(automation_state, search_status, track)}
<section id="opportunities"><div class="section-heading"><h2 class="section-title">Что нашлось в источниках</h2><span class="muted">{_escape(scope)}</span></div>
<p class="muted">Из последних {len(discovered)} записей (до 200); повторные вакансии объединены. «Подходит по тексту» не гарантирует право на работу; «Нужна проверка» не попадает в автоматическую подборку.</p>
{discovery_html}</section>
<section id="queue"><h2 class="section-title">Очередь откликов</h2>
<p class="safety">Утверждённый отклик с адресом работодателя будет отправлен следующим циклом, если Gmail подключён и отправка включена.</p>
<div class="toolbar">
<form method="post" action="/refresh" class="secondary"><input type="hidden" name="csrf" value="{_escape(csrf_token)}">{track_input}<button type="submit">Обновить очередь</button></form>
<form method="post" action="/approve" id="approve-form"><input type="hidden" name="csrf" value="{_escape(csrf_token)}">{track_input}<label class="pick"><input type="checkbox" name="checked" value="yes" required> Я проверил условия вакансий</label><button type="submit">Утвердить выбранные ({draft_ids} доступны)</button></form>
</div>{cards_html}{archive_html}</section>
<section class="search-panel" id="sources"><div class="section-heading"><h2>Автоматический поиск</h2>{search_button}</div>{_search_status(search_status)}
<p class="muted">Greenhouse · Ashby · Himalayas · Remotive · hh. Ошибка одного источника не останавливает остальные. Новые записи могут включать одну вакансию из разных запросов. «Получено описаний» не означает, что все вакансии подходят.</p></section>
<details class="settings-panel" id="settings"><summary>Настройки подключения и отправки</summary>{_automation_panel(automation_state, csrf_token, track, profile)}</details>
<p class="activity-note">Ссылки из писем — отдельные непроверенные предложения. Отклик считается отправленным только после подтверждения Gmail. Результаты неясной отправки не повторяются автоматически.</p>
</main></div></body></html>'''


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
            # WebKit can send Origin: null on POST when every referrer is suppressed.
            # Keep cross-site referrers suppressed, but preserve same-origin form identity.
            self.send_header("Referrer-Policy", "no-referrer" if urlsplit(self.path).path == "/gmail-callback" else "same-origin")
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
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()

        def do_GET(self) -> None:
            if not self._host_is_valid():
                self._respond(400, "Invalid host")
                return
            path = urlsplit(self.path)
            if path.path == "/gmail-callback" and automation and automation.oauth:
                query = parse_qs(path.query)
                try:
                    with repository.automation_lock() as acquired:
                        if not acquired:
                            raise GmailAPIError("Цикл выполняется. Дождись завершения и повтори подключение.")
                        def pause_delivery(email):
                            settings = replace(automation.store.load(), email=email, password="", notifications=False,
                                               mailbox=False, send_applications=False, auto_approve=False)
                            automation.store.save(settings)
                        track, email = automation.oauth.finish(query.get("state", [""])[0],
                            query.get("code", [""])[0], error="error" in query, on_connected=pause_delivery)
                    self._redirect("Google подключён. Проверь резюме, включи нужные этапы и сохрани настройки.", track)
                except GmailAPIError as exc:
                    self._respond(400, '<p>' + _escape(exc) + '</p><a href="/">Вернуться в панель</a>')
                except Exception:
                    self._respond(500, '<p>Не удалось завершить OAuth. Начни подключение из панели заново.</p><a href="/">В панель</a>')
                return
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
                                               automation.snapshot(track=track) if automation else None))
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
                if path in {"/gmail-client", "/gmail-connect", "/gmail-disconnect"}:
                    if automation is None or automation.oauth is None:
                        raise ValueError("OAuth is not configured")
                    with repository.automation_lock() as acquired:
                        if not acquired:
                            raise ValueError("Цикл выполняется. Дождись завершения перед изменением подключения.")
                        if path == "/gmail-client":
                            automation.oauth.configure(data.get("client_json", [""])[0])
                            notice = "OAuth-клиент сохранён. Нажми «Войти с Google»."
                        elif path == "/gmail-disconnect":
                            automation.oauth.disconnect()
                            automation.store.save(replace(automation.store.load(), password=""))
                            notice = "Почта отключена локально. Поиск продолжает работать. Отозвать доступ можно в аккаунте Google."
                        else:
                            import webbrowser
                            url = automation.oauth.begin(self.server.server_port, track)
                            try:
                                opened = webbrowser.open(url, new=2)
                            except Exception:
                                opened = False
                            self._respond(200, '<h2>Вход в Google</h2><p>' +
                                ('Вход открыт в системном браузере.' if opened else 'Не удалось открыть системный браузер автоматически.') +
                                ' Разрешения подтверждаешь ты. Если браузер не открылся, скопируй ссылку в Chrome/Safari, не во встроенный браузер.</p>' +
                                '<textarea readonly rows="8">' + _escape(url) + '</textarea><p><a href="/?track=' + track + '">Вернуться в панель</a></p>')
                            return
                elif path == "/automation-settings":
                    if automation is None:
                        raise ValueError("automation is not configured")
                    previous = automation.store.load()
                    email = data.get("email", [""])[0].strip()
                    oauth_connected = bool(automation.oauth and automation.oauth.status()["connected"])
                    if oauth_connected:
                        email = automation.oauth.status()["email"]
                    password = "".join(data.get("password", [""])[0].split())
                    if not password and email == previous.email:
                        password = previous.password
                    settings = AutomationSettings.from_dict({
                        "email": email, "password": password, "resume_path": data.get("resume_path", [""])[0].strip(),
                        "daily_limit": int(data.get("daily_limit", ["5"])[0]),
                        **{name: data.get(name) == ["yes"] for name in ("notifications", "mailbox", "send_applications", "auto_approve")}})
                    if settings.send_applications:
                        settings.attachment()
                    if settings.connected and not oauth_connected and (settings.email != previous.email or settings.password != previous.password):
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
