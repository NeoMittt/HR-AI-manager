from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from aiohttp import web
from aiogram import Bot

import storage
from app_logging import read_log_tail
from config import BASE_DIR, get_summaries_dir, load_settings, save_settings

logger = logging.getLogger(__name__)

RUNTIME_DIR = BASE_DIR / "runtime"
RUNTIME_DIR.mkdir(exist_ok=True)
ADMIN_URL_FILE = RUNTIME_DIR / "admin_url.txt"
CRASH_LOG_DIR = BASE_DIR / "crash_logs"

STATE_LABELS = {
    "waiting_resume": "Ждет резюме",
    "interviewing": "Собеседование",
    "blocked": "Пауза",
    "waiting_decision": "Ждет решения",
    "completed": "Завершено",
}

DECISION_LABELS = {
    "pending": "Ожидает решения",
    "approved": "Одобрен",
    "rejected": "Отклонен",
}

STATE_OPTIONS = [
    "waiting_resume",
    "interviewing",
    "blocked",
    "waiting_decision",
    "completed",
]

PAGE_TEMPLATE = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>HR Admin</title>
  <style>
    :root {
      --bg: #071018;
      --bg-soft: #0d1924;
      --panel: rgba(13, 25, 36, 0.88);
      --panel-strong: rgba(17, 31, 44, 0.96);
      --line: rgba(144, 182, 210, 0.18);
      --text: #ecf4fa;
      --muted: #91a8bb;
      --accent: #5fd1a8;
      --accent-strong: #1bb67d;
      --warn: #f2b15a;
      --danger: #ff6e6e;
      --shadow: 0 24px 60px rgba(0, 0, 0, 0.32);
      --radius: 20px;
      --radius-sm: 14px;
      --font: "Trebuchet MS", "Segoe UI Variable", "Segoe UI", sans-serif;
      color-scheme: dark;
    }

    * { box-sizing: border-box; }
    html, body { margin: 0; min-height: 100%; background:
      radial-gradient(circle at top left, rgba(95, 209, 168, 0.16), transparent 34%),
      radial-gradient(circle at top right, rgba(87, 144, 255, 0.18), transparent 28%),
      linear-gradient(180deg, #061019 0%, #09131d 48%, #071018 100%);
      color: var(--text); font-family: var(--font); }
    body { min-height: 100vh; }
    a { color: #9edcff; text-decoration: none; }
    a:hover { color: white; }
    button, input, select, textarea {
      font: inherit;
      color: var(--text);
    }
    button {
      border: 0;
      border-radius: 12px;
      padding: 11px 14px;
      cursor: pointer;
      background: linear-gradient(180deg, var(--accent), var(--accent-strong));
      color: #04120c;
      font-weight: 700;
      transition: transform .16s ease, box-shadow .16s ease, opacity .16s ease;
      box-shadow: 0 10px 20px rgba(27, 182, 125, 0.18);
    }
    button:hover { transform: translateY(-1px); }
    button:disabled { opacity: .55; cursor: wait; transform: none; }
    .button-secondary {
      background: rgba(146, 173, 194, 0.12);
      color: var(--text);
      box-shadow: none;
      border: 1px solid rgba(146, 173, 194, 0.18);
    }
    .button-danger {
      background: rgba(255, 110, 110, 0.12);
      color: #ffd4d4;
      border: 1px solid rgba(255, 110, 110, 0.35);
      box-shadow: none;
    }
    .shell {
      width: min(1480px, calc(100% - 32px));
      margin: 0 auto;
      padding: 28px 0 36px;
    }
    .hero {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
      margin-bottom: 22px;
    }
    .hero h1 { margin: 0; font-size: clamp(28px, 3vw, 42px); line-height: 1.05; }
    .hero p { margin: 10px 0 0; color: var(--muted); max-width: 720px; }
    .hero-note {
      min-width: 230px;
      padding: 14px 16px;
      border-radius: 16px;
      background: rgba(11, 23, 33, 0.72);
      border: 1px solid var(--line);
      color: var(--muted);
    }
    .hero-note strong {
      display: block;
      color: var(--text);
      font-size: 18px;
      margin-top: 6px;
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(340px, .8fr);
      gap: 18px;
      align-items: start;
    }
    .stack { display: grid; gap: 18px; }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
      overflow: hidden;
    }
    .panel-inner { padding: 20px; }
    .panel-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 16px;
    }
    .panel-head h2, .panel-head h3, .panel-title {
      margin: 0;
      font-size: 19px;
    }
    .panel-subtitle {
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 14px;
    }
    .toolbar {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    .search-input, .select-input, .text-input, .textarea-input {
      width: 100%;
      border-radius: 14px;
      border: 1px solid rgba(148, 176, 194, 0.18);
      background: rgba(5, 14, 22, 0.78);
      padding: 12px 14px;
      outline: none;
      transition: border-color .16s ease, box-shadow .16s ease;
    }
    .search-input:focus, .select-input:focus, .text-input:focus, .textarea-input:focus {
      border-color: rgba(95, 209, 168, 0.65);
      box-shadow: 0 0 0 3px rgba(95, 209, 168, 0.12);
    }
    .textarea-input {
      min-height: 110px;
      resize: vertical;
      line-height: 1.45;
    }
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .stat-card {
      padding: 16px;
      border-radius: 16px;
      background: rgba(8, 17, 25, 0.8);
      border: 1px solid rgba(148, 176, 194, 0.12);
    }
    .stat-label {
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 7px;
      text-transform: uppercase;
      letter-spacing: .06em;
    }
    .stat-value {
      font-size: 28px;
      font-weight: 700;
    }
    .session-list, .summary-list, .timeline, .message-list {
      display: grid;
      gap: 12px;
    }
    .session-card, .summary-card, .event-card, .message-card {
      padding: 16px;
      border-radius: 16px;
      background: rgba(8, 16, 24, 0.84);
      border: 1px solid rgba(148, 176, 194, 0.14);
    }
    .session-card:hover { border-color: rgba(95, 209, 168, 0.38); }
    .session-top, .message-top, .summary-top {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      margin-bottom: 10px;
    }
    .session-title, .message-role {
      margin: 0;
      font-size: 18px;
      font-weight: 700;
    }
    .muted {
      color: var(--muted);
      font-size: 14px;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: .02em;
      background: rgba(146, 173, 194, 0.12);
      border: 1px solid rgba(146, 173, 194, 0.18);
      color: var(--text);
      white-space: nowrap;
    }
    .badge.waiting_resume { background: rgba(95, 156, 209, 0.14); border-color: rgba(95, 156, 209, 0.3); }
    .badge.interviewing { background: rgba(95, 209, 168, 0.15); border-color: rgba(95, 209, 168, 0.3); }
    .badge.blocked { background: rgba(242, 177, 90, 0.16); border-color: rgba(242, 177, 90, 0.3); color: #ffe7bd; }
    .badge.waiting_decision { background: rgba(129, 168, 255, 0.14); border-color: rgba(129, 168, 255, 0.26); }
    .badge.completed { background: rgba(129, 168, 255, 0.15); border-color: rgba(129, 168, 255, 0.28); }
    .card-meta {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 12px;
      color: var(--muted);
      font-size: 13px;
    }
    .preview-text, .log-box, .content-box {
      white-space: pre-wrap;
      word-break: break-word;
      line-height: 1.5;
      color: #dce7ef;
    }
    .content-box, .log-box {
      padding: 16px;
      border-radius: 16px;
      background: rgba(4, 12, 18, 0.82);
      border: 1px solid rgba(148, 176, 194, 0.14);
      max-height: 390px;
      overflow: auto;
    }
    .grid-two {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .form-grid {
      display: grid;
      gap: 12px;
    }
    .form-actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }
    .session-layout {
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(320px, .9fr);
      gap: 18px;
      align-items: start;
    }
    .info-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .info-card {
      padding: 14px;
      border-radius: 16px;
      background: rgba(8, 16, 24, 0.84);
      border: 1px solid rgba(148, 176, 194, 0.14);
    }
    .info-card strong {
      display: block;
      margin-top: 6px;
      font-size: 18px;
    }
    .checkbox-row {
      display: flex;
      gap: 10px;
      align-items: center;
      color: var(--muted);
      font-size: 14px;
    }
    .status-row {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }
    .status-row .select-input { flex: 1; min-width: 180px; }
    .flash {
      position: fixed;
      right: 18px;
      bottom: 18px;
      min-width: 260px;
      max-width: min(420px, calc(100vw - 36px));
      padding: 14px 16px;
      border-radius: 16px;
      background: rgba(11, 24, 35, 0.96);
      border: 1px solid rgba(95, 209, 168, 0.34);
      box-shadow: var(--shadow);
      opacity: 0;
      transform: translateY(12px);
      pointer-events: none;
      transition: opacity .18s ease, transform .18s ease;
    }
    .flash.show { opacity: 1; transform: translateY(0); }
    .flash.error { border-color: rgba(255, 110, 110, 0.38); }
    .busy-indicator {
      position: fixed;
      top: 18px;
      right: 18px;
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 12px 14px;
      border-radius: 999px;
      background: rgba(11, 24, 35, 0.96);
      border: 1px solid rgba(95, 209, 168, 0.3);
      box-shadow: var(--shadow);
      opacity: 0;
      transform: translateY(-10px);
      pointer-events: none;
      transition: opacity .18s ease, transform .18s ease;
      z-index: 50;
    }
    .busy-indicator.show { opacity: 1; transform: translateY(0); }
    .busy-spinner {
      width: 16px;
      height: 16px;
      border-radius: 50%;
      border: 2px solid rgba(95, 209, 168, 0.18);
      border-top-color: var(--accent);
      animation: spin .8s linear infinite;
      flex: 0 0 auto;
    }
    .busy-text {
      color: var(--text);
      font-size: 13px;
      white-space: nowrap;
    }
    @keyframes spin {
      from { transform: rotate(0deg); }
      to { transform: rotate(360deg); }
    }
    .empty-state {
      padding: 22px;
      border-radius: 16px;
      border: 1px dashed rgba(146, 173, 194, 0.2);
      color: var(--muted);
      text-align: center;
    }
    .top-link {
      display: inline-flex;
      gap: 8px;
      align-items: center;
      color: var(--muted);
      margin-bottom: 12px;
    }
    @media (max-width: 1120px) {
      .layout, .session-layout { grid-template-columns: 1fr; }
      .hero { flex-direction: column; }
    }
    @media (max-width: 720px) {
      .shell { width: min(100% - 20px, 1480px); padding-top: 18px; }
      .panel-inner { padding: 16px; }
      .grid-two { grid-template-columns: 1fr; }
      .toolbar, .form-actions, .status-row { flex-direction: column; align-items: stretch; }
    }
  </style>
</head>
<body>
  __BODY__
  <div id="busyIndicator" class="busy-indicator">
    <span class="busy-spinner"></span>
    <span id="busyText" class="busy-text">Загрузка...</span>
  </div>
  <div id="flash" class="flash"></div>
  <script>
  __SCRIPT__
  </script>
</body>
</html>
"""


def create_app(bot: Bot) -> web.Application:
    app = web.Application(client_max_size=8 * 1024 * 1024)
    app["bot"] = bot
    app.router.add_get("/", dashboard_page)
    app.router.add_get("/session/{user_id:\\d+}", session_page)
    app.router.add_get("/api/dashboard", dashboard_api)
    app.router.add_get("/api/session/{user_id:\\d+}", session_api)
    app.router.add_get("/api/logs", logs_api)
    app.router.add_get("/download/summary/{filename}", download_summary)
    app.router.add_get("/download/crash/{filename}", download_crash)
    app.router.add_get("/download/candidates.csv", download_candidates_csv)
    app.router.add_post("/api/settings/general", save_general_settings)
    app.router.add_post("/api/settings/vacancy", save_vacancy_settings)
    app.router.add_post("/api/session/{user_id:\\d+}/message", send_candidate_message)
    app.router.add_post("/api/session/{user_id:\\d+}/status", set_session_status)
    app.router.add_post("/api/session/{user_id:\\d+}/action", apply_session_action)
    app.router.add_get("/health", healthcheck)
    return app


async def start_admin_server(bot: Bot):
    settings = load_settings()
    host = _normalized_host(settings.get("admin_host", "127.0.0.1"))
    preferred_port = _safe_int(settings.get("admin_port"), 8080)

    app = create_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()

    last_error: OSError | None = None
    for port in range(preferred_port, preferred_port + 20):
        site = web.TCPSite(runner, host, port)
        try:
            await site.start()
        except OSError as error:
            last_error = error
            if _is_bind_conflict(error):
                continue
            await runner.cleanup()
            raise RuntimeError(
                f"Не удалось запустить веб-панель на {host}:{port}: {error}"
            ) from error

        public_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        admin_url = f"http://{public_host}:{port}"
        ADMIN_URL_FILE.write_text(admin_url, encoding="utf-8")
        logger.info("startup event=admin_server_started url=%s", admin_url)
        return runner, admin_url

    await runner.cleanup()
    raise RuntimeError(
        f"Не удалось запустить веб-панель рядом с {host}:{preferred_port}: {last_error}"
    )


async def dashboard_page(request: web.Request) -> web.Response:
    body = """
    <div class="shell">
      <section class="hero">
        <div>
          <h1>HR Control Room</h1>
          <p>Живая админ-панель для найма: сессии кандидатов, настройки вакансии, отправка сообщений от имени бота и понятные логи без постоянного обновления страницы.</p>
        </div>
        <div class="hero-note">
          Последнее обновление
          <strong id="generatedAt">загрузка...</strong>
        </div>
      </section>
      <div id="app-root" class="layout">
        <section class="panel"><div class="panel-inner">Панель загружается...</div></section>
        <section class="panel"><div class="panel-inner">Инициализация интерфейса...</div></section>
      </div>
    </div>
    """
    return web.Response(
        text=_page(body, _dashboard_script()),
        content_type="text/html",
    )


async def session_page(request: web.Request) -> web.Response:
    user_id = request.match_info["user_id"]
    body = f"""
    <div class="shell">
      <a class="top-link" href="/">← Назад к списку сессий</a>
      <section class="hero">
        <div>
          <h1>Карточка кандидата</h1>
          <p>Управляйте статусом, отправляйте сообщения от лица бота и следите за ходом диалога без ручного refresh.</p>
        </div>
        <div class="hero-note">
          Кандидат
          <strong id="candidateHeader">#{user_id}</strong>
        </div>
      </section>
      <div id="app-root" class="session-layout" data-user-id="{user_id}">
        <section class="panel"><div class="panel-inner">Загружаю историю кандидата...</div></section>
        <section class="panel"><div class="panel-inner">Готовлю инструменты управления...</div></section>
      </div>
    </div>
    """
    return web.Response(
        text=_page(body, _session_script()),
        content_type="text/html",
    )


async def dashboard_api(request: web.Request) -> web.Response:
    return web.json_response(_build_dashboard_payload())


async def session_api(request: web.Request) -> web.Response:
    user_id = int(request.match_info["user_id"])
    session = storage.get_session_snapshot(user_id)
    if not session:
        placeholder = storage.create_session(user_id, "Сессия недоступна")
        placeholder["state"] = "waiting_resume"
        return web.json_response(_serialize_session_detail(placeholder))
    return web.json_response(_serialize_session_detail(session))


async def logs_api(request: web.Request) -> web.Response:
    return web.json_response({"logs": read_log_tail(220)})


async def download_summary(request: web.Request) -> web.StreamResponse:
    filename = request.match_info["filename"]
    summaries_dir = get_summaries_dir().resolve()
    target = (summaries_dir / filename).resolve()
    if not target.exists() or target.parent != summaries_dir:
        raise web.HTTPNotFound(text="Summary not found")
    return web.FileResponse(path=target)


async def download_crash(request: web.Request) -> web.StreamResponse:
    filename = request.match_info["filename"]
    target = (CRASH_LOG_DIR / filename).resolve()
    if not target.exists() or target.parent != CRASH_LOG_DIR.resolve():
        raise web.HTTPNotFound(text="Crash log not found")
    return web.FileResponse(path=target)


async def download_candidates_csv(request: web.Request) -> web.StreamResponse:
    sessions = storage.list_sessions()
    rows = _build_ranking_rows(sessions)
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": 'attachment; filename="candidates.csv"',
        },
    )
    await response.prepare(request)

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\n")
    writer.writerow(
        [
            "user_id",
            "candidate",
            "vacancy",
            "state",
            "decision",
            "overall_score",
            "threshold",
            "passed_threshold",
            "updated_at",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row["user_id"],
                row["username"],
                row["vacancy_title"],
                row["state_label"],
                row["decision_label"],
                row["overall_score"],
                row["threshold"],
                "yes" if row["passed_threshold"] else "no",
                row["updated_at"],
            ]
        )

    payload = "\ufeff" + buffer.getvalue()
    await response.write(payload.encode("utf-8"))
    await response.write_eof()
    return response


async def save_general_settings(request: web.Request) -> web.Response:
    payload = await _read_payload(request)
    settings = load_settings()

    telegram_token = str(payload.get("telegram_token", "")).strip()
    openrouter_api_key = str(payload.get("openrouter_api_key", "")).strip()
    if telegram_token:
        settings["telegram_token"] = telegram_token
    if openrouter_api_key:
        settings["openrouter_api_key"] = openrouter_api_key

    settings["openrouter_model"] = (
        str(payload.get("openrouter_model", settings.get("openrouter_model", ""))).strip()
        or settings.get("openrouter_model", "auto")
    )
    settings["openrouter_free_models"] = _split_lines_or_csv(
        payload.get("openrouter_free_models"),
        fallback=settings.get("openrouter_free_models", []),
    )
    settings["employer_ids"] = _parse_int_list(
        payload.get("employer_ids"),
        fallback=settings.get("employer_ids", []),
    )
    settings["sessions_dir"] = (
        str(payload.get("sessions_dir", settings.get("sessions_dir", "sessions"))).strip()
        or "sessions"
    )
    settings["summaries_dir"] = (
        str(payload.get("summaries_dir", settings.get("summaries_dir", "sessions"))).strip()
        or settings["sessions_dir"]
    )
    settings["admin_host"] = (
        str(payload.get("admin_host", settings.get("admin_host", "127.0.0.1"))).strip()
        or "127.0.0.1"
    )
    settings["admin_port"] = _safe_int(payload.get("admin_port"), settings.get("admin_port", 8080))
    settings["block_duration_seconds"] = _safe_int(
        payload.get("block_duration_seconds"),
        settings.get("block_duration_seconds", 10),
    )
    settings["interview_questions_count"] = _safe_int(
        payload.get("interview_questions_count"),
        settings.get("interview_questions_count", 5),
    )

    save_settings(settings)
    logger.info(
        "admin event=settings_saved scope=general model=%s admin_port=%s",
        settings["openrouter_model"],
        settings["admin_port"],
    )
    return web.json_response(
        {"ok": True, "message": "Общие настройки сохранены.", "settings": _settings_snapshot(settings)}
    )


async def save_vacancy_settings(request: web.Request) -> web.Response:
    payload = await _read_payload(request)
    settings = load_settings()
    presets = settings.get("vacancy_presets", {})
    preset_key = str(
        payload.get("preset_key", settings.get("active_vacancy_preset", "technical"))
    ).strip() or settings.get("active_vacancy_preset", "technical")
    if preset_key not in presets:
        preset_key = settings.get("active_vacancy_preset", "technical")
    settings["active_vacancy_preset"] = preset_key
    settings["open_vacancy_keys"] = _split_lines_or_csv(
        payload.get("open_vacancy_keys"),
        fallback=settings.get("open_vacancy_keys", list(presets.keys())),
    )
    vacancy = settings.setdefault("vacancy", {})
    vacancy["title"] = str(payload.get("title", vacancy.get("title", ""))).strip() or "Вакансия"
    vacancy["description"] = str(
        payload.get("description", vacancy.get("description", ""))
    ).strip()
    vacancy["required_skills"] = _split_lines_or_csv(
        payload.get("required_skills"),
        fallback=vacancy.get("required_skills", []),
    )
    vacancy["score_threshold"] = _safe_int(
        payload.get("score_threshold"),
        vacancy.get("score_threshold", 28),
    )
    preset = presets.setdefault(preset_key, {})
    preset["title"] = vacancy["title"]
    preset["description"] = vacancy["description"]
    preset["required_skills"] = list(vacancy["required_skills"])
    preset["score_threshold"] = vacancy["score_threshold"]
    save_settings(settings)
    logger.info(
        "admin event=settings_saved scope=vacancy preset=%s title=%s skills=%s threshold=%s",
        preset_key,
        vacancy["title"],
        len(vacancy["required_skills"]),
        vacancy["score_threshold"],
    )
    return web.json_response(
        {
            "ok": True,
            "message": "Вакансия сохранена.",
            "settings": _settings_snapshot(settings),
        }
    )


async def send_candidate_message(request: web.Request) -> web.Response:
    user_id = int(request.match_info["user_id"])
    payload = await _read_payload(request)
    text = str(payload.get("text", "")).strip()
    add_to_history = _as_bool(payload.get("add_to_history", True))
    if not text:
        return web.json_response({"ok": False, "message": "Сообщение пустое."}, status=400)

    session = await storage.load_session(user_id)
    bot: Bot = request.app["bot"]
    await bot.send_message(user_id, text)

    if add_to_history:
        storage.add_dialog_message(session, "assistant", text, source="employer")
    storage.add_session_event(
        session,
        "employer",
        "manual_message_sent",
        chars=len(text),
        add_to_history=add_to_history,
    )
    await storage.save_session(session)
    logger.info(
        "admin event=manual_message_sent user_id=%s chars=%s add_to_history=%s",
        user_id,
        len(text),
        add_to_history,
    )
    return web.json_response({"ok": True, "message": "Сообщение отправлено."})


async def set_session_status(request: web.Request) -> web.Response:
    user_id = int(request.match_info["user_id"])
    payload = await _read_payload(request)
    new_state = str(payload.get("state", "")).strip()
    if new_state not in STATE_OPTIONS:
        return web.json_response({"ok": False, "message": "Неизвестный статус."}, status=400)

    session = await storage.load_session(user_id)
    _apply_state(session, new_state)
    storage.add_session_event(session, "employer", "status_changed", state=new_state)
    await storage.save_session(session)
    logger.info("admin event=status_changed user_id=%s state=%s", user_id, new_state)
    return web.json_response({"ok": True, "message": "Статус обновлен."})


async def apply_session_action(request: web.Request) -> web.Response:
    user_id = int(request.match_info["user_id"])
    payload = await _read_payload(request)
    action = str(payload.get("action", "")).strip()
    result = await _apply_session_action(user_id, action, bot=request.app["bot"])
    return web.json_response(result, status=200 if result.get("ok") else 400)


async def healthcheck(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def _effective_state(session: dict[str, Any]) -> str:
    raw_state = str(session.get("state", "waiting_resume") or "waiting_resume")
    decision = str(session.get("employer_decision", "pending") or "pending")
    if raw_state == "completed" and decision == "pending":
        return "waiting_decision"
    if raw_state == "waiting_decision" and decision in {"approved", "rejected"}:
        return "completed"
    return raw_state


def _state_label(session: dict[str, Any]) -> str:
    effective_state = _effective_state(session)
    return STATE_LABELS.get(effective_state, effective_state)


def _build_dashboard_payload() -> dict[str, Any]:
    raw_sessions = storage.list_sessions()
    sessions = [_serialize_session_card(session) for session in raw_sessions]
    summaries = [
        {
            "name": path.name,
            "size_kb": round(path.stat().st_size / 1024, 1),
            "updated_at": _format_dt(datetime.fromtimestamp(path.stat().st_mtime)),
        }
        for path in storage.list_summaries()[:20]
    ]
    return {
        "generated_at": _format_dt(datetime.now()),
        "stats": _dashboard_stats(sessions),
        "sessions": sessions,
        "ranking": _build_ranking_rows(raw_sessions),
        "summaries": summaries,
        "settings": _settings_snapshot(load_settings()),
        "latest_crash": _latest_crash_info(),
    }


def _iter_serialized_sessions():
    for session in storage.list_sessions():
        yield _serialize_session_card(session)


def _build_ranking_rows(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for session in sessions:
        if not session.get("candidate_score"):
            continue
        vacancy = session.get("vacancy") or {}
        score = session.get("candidate_score") or {}
        overall = _safe_int(score.get("overall_score"), -1)
        threshold = _safe_int(score.get("threshold"), _safe_int(vacancy.get("score_threshold"), 28))
        rows.append(
            {
                "user_id": session["user_id"],
                "username": session.get("username") or "Без имени",
                "vacancy_title": vacancy.get("title") or "Вакансия",
                "state": _effective_state(session),
                "state_label": _state_label(session),
                "decision": session.get("employer_decision", "pending"),
                "decision_label": DECISION_LABELS.get(
                    session.get("employer_decision", "pending"),
                    session.get("employer_decision", "pending"),
                ),
                "overall_score": overall if overall >= 0 else "-",
                "threshold": threshold,
                "passed_threshold": bool(score.get("passed_threshold")),
                "updated_at": _format_iso(session.get("updated_at")) or "-",
            }
        )

    return sorted(
        rows,
        key=lambda item: (
            -1 if isinstance(item["overall_score"], str) else -int(item["overall_score"]),
            item["updated_at"],
        ),
    )


def _serialize_session_card(session: dict[str, Any]) -> dict[str, Any]:
    history = session.get("interview_history", [])
    answers = sum(
        1
        for item in history
        if item.get("role") == "user" and item.get("source") == "candidate"
    )
    vacancy = session.get("vacancy") or {}
    score = session.get("candidate_score") or {}
    decision = session.get("employer_decision", "pending")
    return {
        "user_id": session["user_id"],
        "username": session.get("username") or "Без имени",
        "state": _effective_state(session),
        "state_label": _state_label(session),
        "started_at": _format_iso(session.get("started_at")),
        "updated_at": _format_iso(session.get("updated_at")),
        "resume_received": bool(session.get("resume_received")),
        "summary_saved": bool(session.get("summary_saved")),
        "off_topic_count": session.get("off_topic_count", 0),
        "answers_count": answers,
        "has_block": bool(session.get("block_until")),
        "resume_preview": _truncate(session.get("resume_text"), 170),
        "analysis_preview": _truncate(session.get("resume_analysis"), 220),
        "vacancy_title": vacancy.get("title", ""),
        "overall_score": score.get("overall_score"),
        "threshold": score.get("threshold", vacancy.get("score_threshold")),
        "passed_threshold": bool(score.get("passed_threshold")),
        "decision": decision,
        "decision_label": DECISION_LABELS.get(decision, decision),
    }


def _serialize_session_detail(session: dict[str, Any]) -> dict[str, Any]:
    summary_path = session.get("summary_path")
    summary_name = Path(summary_path).name if summary_path else None
    vacancy = session.get("vacancy") or {}
    candidate_score = session.get("candidate_score") or {}
    history = []
    for item in session.get("interview_history", []):
        history.append(
            {
                "role": item.get("role"),
                "content": item.get("content", ""),
                "source": item.get("source"),
                "timestamp": _format_iso(item.get("timestamp")),
            }
        )
    events = []
    for item in session.get("session_events", []):
        events.append(
            {
                "at": _format_iso(item.get("at")),
                "actor": item.get("actor"),
                "event": item.get("event"),
                "details": item.get("details", {}),
            }
        )
    return {
        "user_id": session["user_id"],
        "username": session.get("username") or "Без имени",
        "state": _effective_state(session),
        "state_label": _state_label(session),
        "raw_state": session.get("state", "waiting_resume"),
        "started_at": _format_iso(session.get("started_at")),
        "updated_at": _format_iso(session.get("updated_at")),
        "return_state": session.get("return_state"),
        "block_until": _format_iso(session.get("block_until")),
        "block_reason": session.get("block_reason"),
        "permanent_block": bool(session.get("permanent_block")),
        "resume_text": session.get("resume_text") or "",
        "resume_analysis": session.get("resume_analysis") or "",
        "resume_screening": session.get("resume_screening") or {},
        "summary_saved": bool(session.get("summary_saved")),
        "summary_name": summary_name,
        "summary_url": f"/download/summary/{summary_name}" if summary_name else None,
        "off_topic_count": session.get("off_topic_count", 0),
        "vacancy": vacancy,
        "candidate_score": candidate_score,
        "employer_decision": session.get("employer_decision", "pending"),
        "employer_decision_label": DECISION_LABELS.get(
            session.get("employer_decision", "pending"),
            session.get("employer_decision", "pending"),
        ),
        "interview_notes": session.get("interview_notes", []),
        "interview_history": history,
        "session_events": events,
    }


def _dashboard_stats(sessions: list[dict[str, Any]]) -> dict[str, int]:
    stats = {
        "total": len(sessions),
        "waiting_resume": 0,
        "interviewing": 0,
        "blocked": 0,
        "waiting_decision": 0,
        "completed": 0,
    }
    for session in sessions:
        state = session.get("state")
        if state in stats:
            stats[state] += 1
    return stats


def _latest_crash_info() -> dict[str, Any] | None:
    files = sorted(
        CRASH_LOG_DIR.glob("crash_*.log"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not files:
        return None

    target = files[0]
    message = ""
    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
        payload = json.loads("\n".join(raw.splitlines()[1:]))
        message = str(payload.get("error_message", "")).strip()
    except Exception:
        message = "Не удалось прочитать crash-log."

    return {
        "name": target.name,
        "updated_at": _format_dt(datetime.fromtimestamp(target.stat().st_mtime)),
        "message": _truncate(message, 260),
        "url": f"/download/crash/{target.name}",
    }


def _settings_snapshot(settings: dict[str, Any]) -> dict[str, Any]:
    vacancy = settings.get("vacancy", {})
    vacancy_presets = settings.get("vacancy_presets", {})
    return {
        "telegram_token_mask": _mask_secret(settings.get("telegram_token", "")),
        "openrouter_api_key_mask": _mask_secret(settings.get("openrouter_api_key", "")),
        "openrouter_model": settings.get("openrouter_model", ""),
        "openrouter_free_models": "\n".join(settings.get("openrouter_free_models", [])),
        "employer_ids": ", ".join(str(item) for item in settings.get("employer_ids", [])),
        "sessions_dir": settings.get("sessions_dir", "sessions"),
        "summaries_dir": settings.get("summaries_dir", "sessions"),
        "admin_host": settings.get("admin_host", "127.0.0.1"),
        "admin_port": settings.get("admin_port", 8080),
        "block_duration_seconds": settings.get("block_duration_seconds", 10),
        "interview_questions_count": settings.get("interview_questions_count", 5),
        "active_vacancy_preset": settings.get("active_vacancy_preset", "technical"),
        "open_vacancy_keys": settings.get("open_vacancy_keys", []),
        "vacancy_presets": {
            key: {
                "label": value.get("label", key),
                "title": value.get("title", ""),
                "description": value.get("description", ""),
                "required_skills": "\n".join(value.get("required_skills", [])),
                "score_threshold": value.get("score_threshold", 28),
            }
            for key, value in vacancy_presets.items()
        },
        "vacancy": {
            "title": vacancy.get("title", ""),
            "description": vacancy.get("description", ""),
            "required_skills": "\n".join(vacancy.get("required_skills", [])),
            "score_threshold": vacancy.get("score_threshold", 28),
        },
    }


def _candidate_decision_message(decision: str) -> str | None:
    if decision == "approved":
        return (
            "Мы готовы рассмотреть вашу кандидатуру дальше. "
            "Пришлите, пожалуйста, свои контакты, и с вами свяжется наш сотрудник."
        )
    if decision == "rejected":
        return "Спасибо за участие. К сожалению, вы отклонены."
    return None


async def _notify_candidate_decision(bot: Bot, session: dict[str, Any], decision: str):
    text = _candidate_decision_message(decision)
    if not text:
        return
    await bot.send_message(session["user_id"], text)
    storage.add_dialog_message(session, "assistant", text, source="decision_update")
    storage.add_session_event(
        session,
        "employer",
        "candidate_notified_about_decision",
        decision=decision,
    )


async def _apply_session_action(user_id: int, action: str, *, bot: Bot | None = None) -> dict[str, Any]:
    if action == "delete":
        await storage.delete_session(user_id)
        logger.info("admin event=session_deleted user_id=%s", user_id)
        return {"ok": True, "message": "Сессия удалена.", "deleted": True}

    session = await storage.load_session(user_id)
    username = session.get("username")

    if action == "unblock":
        session["state"] = session.get("return_state") or _default_state(session)
        session["return_state"] = None
        session["block_until"] = None
        storage.add_session_event(session, "employer", "session_unblocked")
        await storage.save_session(session)
        logger.info("admin event=session_unblocked user_id=%s", user_id)
        return {"ok": True, "message": "Сессия разблокирована."}

    if action == "reset":
        new_session = storage.create_session(user_id, username)
        storage.add_session_event(new_session, "employer", "session_reset")
        await storage.save_session(new_session)
        logger.info("admin event=session_reset user_id=%s", user_id)
        return {"ok": True, "message": "Сессия сброшена."}

    if action == "clear_summary":
        session["summary_saved"] = False
        session["summary_path"] = None
        storage.add_session_event(session, "employer", "summary_cleared")
        await storage.save_session(session)
        logger.info("admin event=summary_cleared user_id=%s", user_id)
        return {"ok": True, "message": "Ссылка на summary очищена."}

    if action in {"decision_approved", "decision_rejected", "decision_reset"}:
        previous_decision = session.get("employer_decision", "pending")
        decision = {
            "decision_approved": "approved",
            "decision_rejected": "rejected",
            "decision_reset": "pending",
        }[action]
        session["employer_decision"] = decision
        if decision == "pending" and session.get("candidate_score"):
            session["state"] = "waiting_decision"
        elif decision in {"approved", "rejected"}:
            session["state"] = "completed"
        storage.add_session_event(
            session,
            "employer",
            "decision_changed",
            decision=decision,
        )
        if bot and decision in {"approved", "rejected"} and decision != previous_decision:
            try:
                await _notify_candidate_decision(bot, session, decision)
            except Exception as error:
                logger.warning(
                    "admin event=candidate_decision_notify_failed user_id=%s decision=%s error=%s",
                    user_id,
                    decision,
                    error,
                )
        await storage.save_session(session)
        logger.info("admin event=decision_changed user_id=%s decision=%s", user_id, decision)
        return {
            "ok": True,
            "message": f"Решение обновлено: {DECISION_LABELS.get(decision, decision)}.",
        }

    return {"ok": False, "message": "Неизвестное действие."}


def _apply_state(session: dict[str, Any], new_state: str):
    if new_state == "blocked":
        session["return_state"] = session.get("return_state") or _default_state(session)
        session["block_until"] = "2099-12-31T23:59:59"
    else:
        session["return_state"] = None
        session["block_until"] = None
    if new_state == "waiting_decision":
        session["employer_decision"] = "pending"
    session["state"] = new_state


def _default_state(session: dict[str, Any]) -> str:
    if session.get("candidate_score") or session.get("summary_saved"):
        if session.get("employer_decision") in {"approved", "rejected"}:
            return "completed"
        return "waiting_decision"
    if session.get("resume_text"):
        return "interviewing"
    return "waiting_resume"


async def _read_payload(request: web.Request) -> dict[str, Any]:
    if request.content_type == "application/json":
        return await request.json()
    post_data = await request.post()
    return dict(post_data)


def _normalized_host(value: Any) -> str:
    host = str(value or "127.0.0.1").strip()
    return host or "127.0.0.1"


def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return fallback


def _split_lines_or_csv(value: Any, *, fallback: list[str]) -> list[str]:
    if value is None:
        return list(fallback)
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return items or list(fallback)
    text = str(value).replace(",", "\n")
    items = [item.strip() for item in text.splitlines() if item.strip()]
    return items or list(fallback)


def _parse_int_list(value: Any, *, fallback: list[int]) -> list[int]:
    if value is None:
        return list(fallback)
    items = []
    for part in str(value).replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            items.append(int(part))
        except ValueError:
            continue
    return items or list(fallback)


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() not in {"0", "false", "off", "no", ""}


def _mask_secret(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}...{text[-4:]}"


def _format_iso(value: Any) -> str | None:
    if not value:
        return None
    try:
        return _format_dt(datetime.fromisoformat(str(value)))
    except ValueError:
        return str(value)


def _format_dt(value: datetime) -> str:
    return value.strftime("%d.%m.%Y %H:%M:%S")


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _is_bind_conflict(error: OSError) -> bool:
    text = str(error).lower()
    return getattr(error, "winerror", None) == 10048 or "10048" in text


def _page(body: str, script: str) -> str:
    return PAGE_TEMPLATE.replace("__BODY__", body).replace("__SCRIPT__", script)


def _dashboard_script() -> str:
    return """
const root = document.getElementById('app-root');
const generatedAt = document.getElementById('generatedAt');
const busyIndicator = document.getElementById('busyIndicator');
const busyText = document.getElementById('busyText');
let dashboardData = null;
let shellRendered = false;
let pendingRequests = 0;

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

function showFlash(message, isError = false) {
  const flash = document.getElementById('flash');
  flash.textContent = message;
  flash.className = `flash show${isError ? ' error' : ''}`;
  window.clearTimeout(showFlash._timer);
  showFlash._timer = window.setTimeout(() => {
    flash.className = 'flash';
  }, 3200);
}

function setBusy(isBusy, message = 'Загрузка данных...') {
  if (!busyIndicator || !busyText) {
    return;
  }
  busyText.textContent = message;
  busyIndicator.classList.toggle('show', isBusy);
}

async function fetchJson(url, options = {}) {
  const { loadingText = 'Загрузка данных...', ...fetchOptions } = options;
  pendingRequests += 1;
  const busyTimer = window.setTimeout(() => setBusy(true, loadingText), 220);
  try {
    const response = await fetch(url, fetchOptions);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.message || `HTTP ${response.status}`);
    }
    return data;
  } finally {
    window.clearTimeout(busyTimer);
    pendingRequests = Math.max(0, pendingRequests - 1);
    if (!pendingRequests) {
      setBusy(false);
    }
  }
}

function renderShell(settings) {
  root.innerHTML = `
    <section class="panel">
      <div class="panel-inner">
        <div class="panel-head">
          <div>
            <h2>Сессии кандидатов</h2>
            <p class="panel-subtitle">Фильтруйте, открывайте карточку кандидата и следите за изменениями без ручного обновления.</p>
          </div>
          <div class="toolbar">
            <input id="searchInput" class="search-input" placeholder="Поиск по имени, id или тексту" />
            <select id="statusFilter" class="select-input">
              <option value="all">Все статусы</option>
              <option value="waiting_resume">Ждет резюме</option>
              <option value="interviewing">Собеседование</option>
              <option value="blocked">Пауза</option>
              <option value="waiting_decision">Ждет решения</option>
              <option value="completed">Завершено</option>
            </select>
          </div>
        </div>
        <div id="statsGrid" class="stats-grid"></div>
        <div id="sessionsList" class="session-list"></div>
      </div>
    </section>
    <div class="stack">
      <section class="panel">
        <div class="panel-inner">
          <div class="panel-head">
            <div>
              <h2>Общие настройки</h2>
              <p class="panel-subtitle">Токены можно оставить пустыми, тогда текущие значения сохранятся.</p>
            </div>
          </div>
          <form id="generalForm" class="form-grid">
            <div class="grid-two">
              <label><div class="muted">OpenRouter model</div><input class="text-input" name="openrouter_model" value="${escapeHtml(settings.openrouter_model)}"></label>
              <label><div class="muted">Employer IDs</div><input class="text-input" name="employer_ids" value="${escapeHtml(settings.employer_ids)}"></label>
            </div>
            <label><div class="muted">Fallback free models</div><textarea class="textarea-input" name="openrouter_free_models">${escapeHtml(settings.openrouter_free_models)}</textarea></label>
            <div class="grid-two">
              <label><div class="muted">Telegram token</div><input class="text-input" name="telegram_token" placeholder="Текущее: ${escapeHtml(settings.telegram_token_mask)}"></label>
              <label><div class="muted">OpenRouter API key</div><input class="text-input" name="openrouter_api_key" placeholder="Текущий: ${escapeHtml(settings.openrouter_api_key_mask)}"></label>
            </div>
            <div class="grid-two">
              <label><div class="muted">Sessions dir</div><input class="text-input" name="sessions_dir" value="${escapeHtml(settings.sessions_dir)}"></label>
              <label><div class="muted">Summaries dir</div><input class="text-input" name="summaries_dir" value="${escapeHtml(settings.summaries_dir)}"></label>
            </div>
            <div class="grid-two">
              <label><div class="muted">Admin host</div><input class="text-input" name="admin_host" value="${escapeHtml(String(settings.admin_host))}"></label>
              <label><div class="muted">Admin port</div><input class="text-input" name="admin_port" value="${escapeHtml(String(settings.admin_port))}"></label>
            </div>
            <div class="grid-two">
              <label><div class="muted">Block timeout (sec)</div><input class="text-input" name="block_duration_seconds" value="${escapeHtml(String(settings.block_duration_seconds))}"></label>
              <label><div class="muted">Interview questions</div><input class="text-input" name="interview_questions_count" value="${escapeHtml(String(settings.interview_questions_count))}"></label>
            </div>
            <div class="form-actions">
              <button type="submit">Сохранить настройки</button>
            </div>
          </form>
        </div>
      </section>
      <section class="panel">
        <div class="panel-inner">
          <div class="panel-head">
            <div>
              <h2>Сравнение кандидатов</h2>
              <p class="panel-subtitle">Все кандидаты с интервью и score на одном экране. Сортировка идет по итоговому баллу.</p>
            </div>
            <a class="badge completed" href="/download/candidates.csv">экспорт CSV</a>
          </div>
          <div id="rankingList" class="summary-list"></div>
        </div>
      </section>
      <section class="panel">
        <div class="panel-inner">
          <div class="panel-head">
            <div>
              <h2>Вакансия</h2>
              <p class="panel-subtitle">Можно быстро переключать направление и при необходимости вручную донастроить вакансию.</p>
            </div>
          </div>
          <form id="vacancyForm" class="form-grid">
            <label>
              <div class="muted">Направление / пресет</div>
              <select class="text-input" name="preset_key" id="vacancyPresetSelect">
                ${Object.entries(settings.vacancy_presets || {}).map(([key, preset]) => `
                  <option value="${escapeHtml(key)}" ${key === settings.active_vacancy_preset ? 'selected' : ''}>${escapeHtml(preset.label || key)}</option>
                `).join('')}
              </select>
            </label>
            <label><div class="muted">Название</div><input class="text-input" name="title" value="${escapeHtml(settings.vacancy.title)}"></label>
            <label><div class="muted">Описание</div><textarea class="textarea-input" name="description">${escapeHtml(settings.vacancy.description)}</textarea></label>
            <label><div class="muted">Ключевые навыки</div><textarea class="textarea-input" name="required_skills">${escapeHtml(settings.vacancy.required_skills)}</textarea></label>
            <label><div class="muted">Score threshold</div><input class="text-input" name="score_threshold" value="${escapeHtml(String(settings.vacancy.score_threshold ?? 28))}"></label>
            <div>
              <div class="muted" style="margin-bottom:8px;">Открытые вакансии для кандидата</div>
              <div class="form-grid">
                ${Object.entries(settings.vacancy_presets || {}).map(([key, preset]) => `
                  <label class="checkbox-row">
                    <input type="checkbox" name="open_vacancy_keys" value="${escapeHtml(key)}" ${(settings.open_vacancy_keys || []).includes(key) ? 'checked' : ''}>
                    <span>${escapeHtml(preset.label || key)}</span>
                  </label>
                `).join('')}
              </div>
            </div>
            <div class="form-actions">
              <button type="submit">Сохранить вакансию</button>
            </div>
          </form>
        </div>
      </section>
      <section class="panel">
        <div class="panel-inner">
          <div class="panel-head">
            <div>
              <h2>Сводки и crash-log</h2>
              <p class="panel-subtitle">Быстрый доступ к summary-файлам и последнему падению.</p>
            </div>
          </div>
          <div id="crashBox" class="summary-list"></div>
          <div id="summaryList" class="summary-list"></div>
        </div>
      </section>
      <section class="panel">
        <div class="panel-inner">
          <div class="panel-head">
            <div>
              <h2>Логи приложения</h2>
              <p class="panel-subtitle">Лента обновляется автоматически, но кнопку тоже оставил для ручного refresh.</p>
            </div>
            <button type="button" id="refreshLogsBtn" class="button-secondary">Обновить</button>
          </div>
          <pre id="logsPane" class="log-box">Загрузка логов...</pre>
        </div>
      </section>
    </div>
  `;

  document.getElementById('searchInput').addEventListener('input', renderSessions);
  document.getElementById('statusFilter').addEventListener('change', renderSessions);
  document.getElementById('refreshLogsBtn').addEventListener('click', loadLogs);
  document.getElementById('generalForm').addEventListener('submit', saveGeneralForm);
  document.getElementById('vacancyForm').addEventListener('submit', saveVacancyForm);
  const presetSelect = document.getElementById('vacancyPresetSelect');
  if (presetSelect) {
    presetSelect.addEventListener('change', applyVacancyPreset);
  }
  shellRendered = true;
}

function renderStats() {
  const stats = dashboardData.stats;
  document.getElementById('statsGrid').innerHTML = `
    <div class="stat-card"><div class="stat-label">Всего</div><div class="stat-value">${stats.total}</div></div>
    <div class="stat-card"><div class="stat-label">Ждут резюме</div><div class="stat-value">${stats.waiting_resume}</div></div>
    <div class="stat-card"><div class="stat-label">Интервью</div><div class="stat-value">${stats.interviewing}</div></div>
    <div class="stat-card"><div class="stat-label">Пауза</div><div class="stat-value">${stats.blocked}</div></div>
    <div class="stat-card"><div class="stat-label">Ждут решения</div><div class="stat-value">${stats.waiting_decision}</div></div>
    <div class="stat-card"><div class="stat-label">Завершено</div><div class="stat-value">${stats.completed}</div></div>
  `;
}

function renderSessions() {
  const host = document.getElementById('sessionsList');
  const query = document.getElementById('searchInput').value.trim().toLowerCase();
  const statusFilter = document.getElementById('statusFilter').value;
  const sessions = dashboardData.sessions.filter((session) => {
    const haystack = `${session.username} ${session.user_id} ${session.resume_preview} ${session.analysis_preview}`.toLowerCase();
    const matchQuery = !query || haystack.includes(query);
    const matchStatus = statusFilter === 'all' || session.state === statusFilter;
    return matchQuery && matchStatus;
  });

  if (!sessions.length) {
    host.innerHTML = '<div class="empty-state">По текущему фильтру сессий не найдено.</div>';
    return;
  }

  host.innerHTML = sessions.map((session) => `
    <a class="session-card" href="/session/${session.user_id}">
      <div class="session-top">
        <div>
          <h3 class="session-title">${escapeHtml(session.username)}</h3>
          <div class="muted">ID ${session.user_id}</div>
        </div>
        <span class="badge ${session.state}">${escapeHtml(session.state_label)}</span>
      </div>
      <div class="preview-text">${escapeHtml(session.analysis_preview || session.resume_preview || 'Данных пока мало.')}</div>
      <div class="card-meta">
        <span>Обновлено: ${escapeHtml(session.updated_at || '-')}</span>
        <span>Вакансия: ${escapeHtml(session.vacancy_title || '-')}</span>
        <span>Score: ${escapeHtml(String(session.overall_score ?? '-'))}/${escapeHtml(String(session.threshold ?? '-'))}</span>
        <span>Решение: ${escapeHtml(session.decision_label || '—')}</span>
        <span>Ответов: ${session.answers_count}</span>
        <span>Off-topic: ${session.off_topic_count}</span>
        <span>${session.summary_saved ? 'summary сохранен' : 'summary еще нет'}</span>
      </div>
    </a>
  `).join('');
}

function renderRanking() {
  const host = document.getElementById('rankingList');
  const ranking = dashboardData.ranking || [];
  if (!host) {
    return;
  }
  if (!ranking.length) {
    host.innerHTML = '<div class="empty-state">Рейтинг появится после первых интервью и scoring.</div>';
    return;
  }
  host.innerHTML = ranking.map((item, index) => `
    <a class="summary-card" href="/session/${item.user_id}">
      <div class="summary-top">
        <div>
          <strong>#${index + 1} ${escapeHtml(item.username)}</strong>
          <div class="muted">${escapeHtml(item.vacancy_title || 'Вакансия')} • ${escapeHtml(item.updated_at || '-')}</div>
        </div>
        <span class="badge ${item.passed_threshold ? 'completed' : 'blocked'}">${escapeHtml(String(item.overall_score))}/${escapeHtml(String(item.threshold))}</span>
      </div>
      <div class="card-meta">
        <span>${escapeHtml(item.state_label || '-')}</span>
        <span>${escapeHtml(item.decision_label || '-')}</span>
        <span>${item.passed_threshold ? 'порог пройден' : 'ниже порога'}</span>
      </div>
    </a>
  `).join('');
}

function renderSummaries() {
  const crashBox = document.getElementById('crashBox');
  const summaryList = document.getElementById('summaryList');
  const latestCrash = dashboardData.latest_crash;

  crashBox.innerHTML = latestCrash ? `
    <div class="summary-card">
      <div class="summary-top">
        <div>
          <h3 class="panel-title">Последний crash-log</h3>
          <div class="muted">${escapeHtml(latestCrash.updated_at)}</div>
        </div>
        <a class="badge blocked" href="${latestCrash.url}">скачать</a>
      </div>
      <div class="preview-text">${escapeHtml(latestCrash.message)}</div>
    </div>
  ` : '<div class="empty-state">Crash-логов пока нет.</div>';

  if (!dashboardData.summaries.length) {
    summaryList.innerHTML = '<div class="empty-state">Summary-файлы появятся после завершенных интервью.</div>';
    return;
  }

  summaryList.innerHTML = dashboardData.summaries.map((item) => `
    <a class="summary-card" href="/download/summary/${encodeURIComponent(item.name)}">
      <div class="summary-top">
        <strong>${escapeHtml(item.name)}</strong>
        <span class="badge completed">${item.size_kb} KB</span>
      </div>
      <div class="muted">Обновлено: ${escapeHtml(item.updated_at)}</div>
    </a>
  `).join('');
}

function syncSettingsForms() {
  const settings = dashboardData.settings || {};
  const generalForm = document.getElementById('generalForm');
  if (generalForm) {
    for (const [key, value] of Object.entries({
      openrouter_model: settings.openrouter_model || '',
      openrouter_free_models: settings.openrouter_free_models || '',
      employer_ids: settings.employer_ids || '',
      sessions_dir: settings.sessions_dir || '',
      summaries_dir: settings.summaries_dir || '',
      admin_host: settings.admin_host || '',
      admin_port: String(settings.admin_port ?? ''),
      block_duration_seconds: String(settings.block_duration_seconds ?? ''),
      interview_questions_count: String(settings.interview_questions_count ?? ''),
    })) {
      if (generalForm.elements[key]) {
        generalForm.elements[key].value = value;
      }
    }
  }

  const vacancyForm = document.getElementById('vacancyForm');
  if (vacancyForm) {
    if (vacancyForm.elements.preset_key) {
      vacancyForm.elements.preset_key.value = settings.active_vacancy_preset || '';
    }
    if (vacancyForm.elements.title) {
      vacancyForm.elements.title.value = settings.vacancy?.title || '';
    }
    if (vacancyForm.elements.description) {
      vacancyForm.elements.description.value = settings.vacancy?.description || '';
    }
    if (vacancyForm.elements.required_skills) {
      vacancyForm.elements.required_skills.value = settings.vacancy?.required_skills || '';
    }
    if (vacancyForm.elements.score_threshold) {
      vacancyForm.elements.score_threshold.value = String(settings.vacancy?.score_threshold ?? 28);
    }
    const openVacancyKeys = new Set(settings.open_vacancy_keys || []);
    vacancyForm.querySelectorAll('input[name="open_vacancy_keys"]').forEach((checkbox) => {
      checkbox.checked = openVacancyKeys.has(checkbox.value);
    });
  }
}

function applyVacancyPreset(event) {
  const presetKey = event.currentTarget.value;
  const preset = dashboardData.settings?.vacancy_presets?.[presetKey];
  const vacancyForm = document.getElementById('vacancyForm');
  if (!preset || !vacancyForm) {
    return;
  }
  if (vacancyForm.elements.title) {
    vacancyForm.elements.title.value = preset.title || '';
  }
  if (vacancyForm.elements.description) {
    vacancyForm.elements.description.value = preset.description || '';
  }
  if (vacancyForm.elements.required_skills) {
    vacancyForm.elements.required_skills.value = preset.required_skills || '';
  }
  if (vacancyForm.elements.score_threshold) {
    vacancyForm.elements.score_threshold.value = String(preset.score_threshold ?? 28);
  }
}

async function loadLogs() {
  try {
    const data = await fetchJson('/api/logs', { loadingText: 'Обновляю логи...' });
    document.getElementById('logsPane').textContent = data.logs || 'Логи пока пустые.';
  } catch (error) {
    showFlash(`Не удалось загрузить логи: ${error.message}`, true);
  }
}

async function saveGeneralForm(event) {
  event.preventDefault();
  try {
    const form = event.currentTarget;
    const payload = Object.fromEntries(new FormData(form).entries());
    const response = await fetchJson('/api/settings/general', {
      loadingText: 'Сохраняю общие настройки...',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    showFlash(response.message || 'Настройки сохранены.');
    await refreshDashboard(true);
  } catch (error) {
    showFlash(`Не удалось сохранить настройки: ${error.message}`, true);
  }
}

async function saveVacancyForm(event) {
  event.preventDefault();
  try {
    const form = event.currentTarget;
    const formData = new FormData(form);
    const payload = Object.fromEntries(formData.entries());
    payload.open_vacancy_keys = formData.getAll('open_vacancy_keys');
    const response = await fetchJson('/api/settings/vacancy', {
      loadingText: 'Сохраняю вакансию...',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    showFlash(response.message || 'Вакансия сохранена.');
    await refreshDashboard(true);
  } catch (error) {
    showFlash(`Не удалось сохранить вакансию: ${error.message}`, true);
  }
}

async function refreshDashboard(forceFormSync = false) {
  try {
    dashboardData = await fetchJson('/api/dashboard', { loadingText: 'Обновляю панель...' });
    generatedAt.textContent = dashboardData.generated_at;
    if (!shellRendered) {
      renderShell(dashboardData.settings);
      loadLogs();
      forceFormSync = true;
    }
    if (forceFormSync) {
      syncSettingsForms();
    }
    renderStats();
    renderSessions();
    renderRanking();
    renderSummaries();
  } catch (error) {
    showFlash(`Не удалось обновить панель: ${error.message}`, true);
  }
}

refreshDashboard(true);
window.setInterval(refreshDashboard, 4000);
window.setInterval(() => {
  if (shellRendered) {
    loadLogs();
  }
}, 5000);
"""


def _session_script() -> str:
    return """
const root = document.getElementById('app-root');
const userId = root.dataset.userId;
const candidateHeader = document.getElementById('candidateHeader');
const busyIndicator = document.getElementById('busyIndicator');
const busyText = document.getElementById('busyText');
let sessionData = null;
let shellRendered = false;
let pendingRequests = 0;

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

function showFlash(message, isError = false) {
  const flash = document.getElementById('flash');
  flash.textContent = message;
  flash.className = `flash show${isError ? ' error' : ''}`;
  window.clearTimeout(showFlash._timer);
  showFlash._timer = window.setTimeout(() => {
    flash.className = 'flash';
  }, 3200);
}

function setBusy(isBusy, message = 'Загрузка данных...') {
  if (!busyIndicator || !busyText) {
    return;
  }
  busyText.textContent = message;
  busyIndicator.classList.toggle('show', isBusy);
}

async function fetchJson(url, options = {}) {
  const { loadingText = 'Загрузка данных...', ...fetchOptions } = options;
  pendingRequests += 1;
  const busyTimer = window.setTimeout(() => setBusy(true, loadingText), 220);
  try {
    const response = await fetch(url, fetchOptions);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.message || `HTTP ${response.status}`);
    }
    return data;
  } finally {
    window.clearTimeout(busyTimer);
    pendingRequests = Math.max(0, pendingRequests - 1);
    if (!pendingRequests) {
      setBusy(false);
    }
  }
}

function renderShell() {
  root.innerHTML = `
    <section class="panel">
      <div class="panel-inner">
        <div class="panel-head">
          <div>
            <h2 id="sessionTitle">Кандидат #${userId}</h2>
            <p class="panel-subtitle" id="sessionSubtitle">Подтягиваю актуальные данные по сессии...</p>
          </div>
          <span id="sessionBadge" class="badge waiting_resume">загрузка</span>
        </div>
        <div id="infoGrid" class="info-grid"></div>
        <div class="stack">
          <div class="panel" style="box-shadow:none">
            <div class="panel-inner">
              <div class="panel-head">
                <div>
                  <h3>История диалога</h3>
                  <p class="panel-subtitle">Сообщения кандидата, AI и ручные сообщения из админки.</p>
                </div>
              </div>
              <div id="historyList" class="message-list"></div>
            </div>
          </div>
          <div class="panel" style="box-shadow:none">
            <div class="panel-inner">
              <div class="panel-head">
                <div>
                  <h3>События сессии</h3>
                  <p class="panel-subtitle">Статусы, блокировки, ручные действия и summary.</p>
                </div>
              </div>
              <div id="eventsList" class="timeline"></div>
            </div>
          </div>
          <div class="panel" style="box-shadow:none">
            <div class="panel-inner">
              <div class="panel-head">
                <div>
                  <h3>Резюме</h3>
                  <p class="panel-subtitle">Исходный текст, который прислал кандидат.</p>
                </div>
              </div>
              <pre id="resumeBox" class="content-box"></pre>
            </div>
          </div>
          <div class="panel" style="box-shadow:none">
            <div class="panel-inner">
              <div class="panel-head">
                <div>
                  <h3>Анализ резюме</h3>
                  <p class="panel-subtitle">AI-анализ виден работодателю, но не кандидату.</p>
                </div>
              </div>
              <pre id="analysisBox" class="content-box"></pre>
            </div>
          </div>
        </div>
      </div>
    </section>
    <div class="stack">
      <section class="panel">
        <div class="panel-inner">
          <div class="panel-head">
            <div>
              <h2>Управление сессией</h2>
              <p class="panel-subtitle">Меняйте статус, снимайте блокировку или сбрасывайте диалог.</p>
            </div>
          </div>
          <div class="form-grid">
            <div class="status-row">
              <select id="statusSelect" class="select-input">
                <option value="waiting_resume">Ждет резюме</option>
                <option value="interviewing">Собеседование</option>
                <option value="blocked">Пауза</option>
                <option value="waiting_decision">Ждет решения</option>
                <option value="completed">Завершено</option>
              </select>
              <button type="button" id="applyStatusBtn">Сменить статус</button>
            </div>
            <div class="form-actions">
              <button type="button" id="approveBtn" class="button-secondary">Одобрить</button>
              <button type="button" id="rejectBtn" class="button-secondary">Отклонить</button>
              <button type="button" id="decisionResetBtn" class="button-secondary">Сбросить решение</button>
              <button type="button" id="unblockBtn" class="button-secondary">Разблокировать</button>
              <button type="button" id="resetBtn" class="button-secondary">Сбросить</button>
              <button type="button" id="clearSummaryBtn" class="button-secondary">Очистить summary</button>
              <button type="button" id="deleteBtn" class="button-danger">Удалить сессию</button>
            </div>
          </div>
        </div>
      </section>
      <section class="panel">
        <div class="panel-inner">
          <div class="panel-head">
            <div>
              <h2>Сообщение кандидату</h2>
              <p class="panel-subtitle">Отправка идет от имени Telegram-бота прямо кандидату.</p>
            </div>
          </div>
          <form id="messageForm" class="form-grid">
            <textarea id="messageText" class="textarea-input" name="text" placeholder="Например: Спасибо, мы изучили ваше резюме. Уточните, пожалуйста, когда вам удобно выйти на связь."></textarea>
            <label class="checkbox-row">
              <input type="checkbox" id="addToHistory" name="add_to_history" checked>
              <span>Добавить сообщение в историю интервью</span>
            </label>
            <div class="form-actions">
              <button type="submit">Отправить кандидату</button>
            </div>
          </form>
        </div>
      </section>
      <section class="panel">
        <div class="panel-inner">
          <div class="panel-head">
            <div>
              <h2>Summary</h2>
              <p class="panel-subtitle">Итоговая сводка для работодателя по завершенному интервью.</p>
            </div>
          </div>
          <div id="summaryCard"></div>
        </div>
      </section>
    </div>
  `;

  document.getElementById('messageForm').addEventListener('submit', sendMessage);
  document.getElementById('applyStatusBtn').addEventListener('click', applyStatus);
  document.getElementById('approveBtn').addEventListener('click', () => runAction('decision_approved'));
  document.getElementById('rejectBtn').addEventListener('click', () => runAction('decision_rejected'));
  document.getElementById('decisionResetBtn').addEventListener('click', () => runAction('decision_reset'));
  document.getElementById('unblockBtn').addEventListener('click', () => runAction('unblock'));
  document.getElementById('resetBtn').addEventListener('click', () => runAction('reset'));
  document.getElementById('clearSummaryBtn').addEventListener('click', () => runAction('clear_summary'));
  document.getElementById('deleteBtn').addEventListener('click', deleteSession);
  shellRendered = true;
}

function renderInfo() {
  const score = sessionData.candidate_score || {};
  const vacancy = sessionData.vacancy || {};
  candidateHeader.textContent = `${sessionData.username} (#${sessionData.user_id})`;
  document.getElementById('sessionTitle').textContent = `${sessionData.username} (#${sessionData.user_id})`;
  document.getElementById('sessionSubtitle').textContent = `Последнее обновление: ${sessionData.updated_at || '-'}${sessionData.block_until ? ` • блок до ${sessionData.block_until}` : ''}`;
  const badge = document.getElementById('sessionBadge');
  badge.className = `badge ${sessionData.state}`;
  badge.textContent = sessionData.state_label;
  document.getElementById('statusSelect').value = sessionData.raw_state || sessionData.state;
  document.getElementById('infoGrid').innerHTML = `
    <div class="info-card"><div class="muted">Статус</div><strong>${escapeHtml(sessionData.state_label)}</strong></div>
    <div class="info-card"><div class="muted">Старт</div><strong>${escapeHtml(sessionData.started_at || '-')}</strong></div>
    <div class="info-card"><div class="muted">Обновлено</div><strong>${escapeHtml(sessionData.updated_at || '-')}</strong></div>
    <div class="info-card"><div class="muted">Вакансия</div><strong>${escapeHtml(vacancy.title || '-')}</strong></div>
    <div class="info-card"><div class="muted">Score</div><strong>${escapeHtml(String(score.overall_score ?? '-'))}/${escapeHtml(String(score.threshold ?? vacancy.score_threshold ?? '-'))}</strong></div>
    <div class="info-card"><div class="muted">Решение</div><strong>${escapeHtml(sessionData.employer_decision_label || '—')}</strong></div>
    <div class="info-card"><div class="muted">Off-topic</div><strong>${sessionData.off_topic_count}</strong></div>
    <div class="info-card"><div class="muted">Сообщений в истории</div><strong>${sessionData.interview_history.length}</strong></div>
    <div class="info-card"><div class="muted">Summary</div><strong>${sessionData.summary_saved ? 'готово' : 'нет'}</strong></div>
  `;
}

function renderHistory() {
  const host = document.getElementById('historyList');
  if (!sessionData.interview_history.length) {
    host.innerHTML = '<div class="empty-state">История диалога пока пустая.</div>';
    return;
  }
  host.innerHTML = sessionData.interview_history.map((item) => {
    const roleLabel = item.role === 'user' ? 'Кандидат' : 'Бот / HR';
    const source = item.source ? ` • ${item.source}` : '';
    return `
      <div class="message-card">
        <div class="message-top">
          <div>
            <div class="message-role">${roleLabel}</div>
            <div class="muted">${escapeHtml(item.timestamp || '-')}${escapeHtml(source)}</div>
          </div>
          <span class="badge ${item.role === 'user' ? 'waiting_resume' : 'interviewing'}">${escapeHtml(item.role)}</span>
        </div>
        <div class="preview-text">${escapeHtml(item.content || '')}</div>
      </div>
    `;
  }).join('');
}

function renderEvents() {
  const host = document.getElementById('eventsList');
  if (!sessionData.session_events.length) {
    host.innerHTML = '<div class="empty-state">События еще не накопились.</div>';
    return;
  }
  const items = [...sessionData.session_events].reverse();
  host.innerHTML = items.map((event) => `
    <div class="event-card">
      <div class="message-top">
        <div>
          <div class="message-role">${escapeHtml(event.event || 'event')}</div>
          <div class="muted">${escapeHtml(event.at || '-')} • ${escapeHtml(event.actor || '-')}</div>
        </div>
      </div>
      <div class="preview-text">${escapeHtml(JSON.stringify(event.details || {}, null, 2))}</div>
    </div>
  `).join('');
}

function renderTexts() {
  const score = sessionData.candidate_score || {};
  const vacancy = sessionData.vacancy || {};
  document.getElementById('resumeBox').textContent = sessionData.resume_text || 'Резюме пока не загружено.';
  document.getElementById('analysisBox').textContent = sessionData.resume_analysis || 'Анализ еще не готов.';
  document.getElementById('summaryCard').innerHTML = sessionData.summary_saved && sessionData.summary_url ? `
    <div class="summary-card">
      <div class="summary-top">
        <div>
          <strong>${escapeHtml(sessionData.summary_name || 'summary')}</strong>
          <div class="muted">Файл доступен для скачивания.</div>
        </div>
        <a class="badge completed" href="${sessionData.summary_url}">скачать</a>
      </div>
      <div class="card-meta">
        <span>${escapeHtml(vacancy.title || 'Вакансия')}</span>
        <span>Score: ${escapeHtml(String(score.overall_score ?? '-'))}/${escapeHtml(String(score.threshold ?? vacancy.score_threshold ?? '-'))}</span>
        <span>${escapeHtml(sessionData.employer_decision_label || '—')}</span>
      </div>
      <div class="preview-text">${escapeHtml(score.employer_summary || 'Комментарий по score появится после оценки кандидата.')}</div>
    </div>
  ` : '<div class="empty-state">Summary пока нет.</div>';
}

async function sendMessage(event) {
  event.preventDefault();
  try {
    const textField = document.getElementById('messageText');
    const addToHistory = document.getElementById('addToHistory').checked;
    const payload = {
      text: textField.value,
      add_to_history: addToHistory,
    };
    const response = await fetchJson(`/api/session/${userId}/message`, {
      loadingText: 'Отправляю сообщение кандидату...',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    textField.value = '';
    showFlash(response.message || 'Сообщение отправлено.');
    await refreshSession();
  } catch (error) {
    showFlash(`Не удалось отправить сообщение: ${error.message}`, true);
  }
}

async function applyStatus() {
  try {
    const newState = document.getElementById('statusSelect').value;
    const response = await fetchJson(`/api/session/${userId}/status`, {
      loadingText: 'Меняю статус...',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ state: newState }),
    });
    showFlash(response.message || 'Статус обновлен.');
    await refreshSession();
  } catch (error) {
    showFlash(`Не удалось сменить статус: ${error.message}`, true);
  }
}

async function runAction(action) {
  try {
    const response = await fetchJson(`/api/session/${userId}/action`, {
      loadingText: 'Выполняю действие...',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action }),
    });
    showFlash(response.message || 'Действие выполнено.');
    await refreshSession();
  } catch (error) {
    showFlash(`Не удалось выполнить действие: ${error.message}`, true);
  }
}

async function deleteSession() {
  const confirmed = window.confirm('Удалить сессию кандидата? Это удалит json-файл с историей.');
  if (!confirmed) {
    return;
  }
  try {
    const response = await fetchJson(`/api/session/${userId}/action`, {
      loadingText: 'Удаляю сессию...',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'delete' }),
    });
    showFlash(response.message || 'Сессия удалена.');
    window.setTimeout(() => {
      window.location.href = '/';
    }, 500);
  } catch (error) {
    showFlash(`Не удалось удалить сессию: ${error.message}`, true);
  }
}

async function refreshSession() {
  try {
    sessionData = await fetchJson(`/api/session/${userId}`, { loadingText: 'Обновляю карточку кандидата...' });
    if (!shellRendered) {
      renderShell();
    }
    renderInfo();
    renderHistory();
    renderEvents();
    renderTexts();
  } catch (error) {
    showFlash(`Не удалось обновить карточку: ${error.message}`, true);
  }
}

refreshSession();
window.setInterval(refreshSession, 4000);
"""
