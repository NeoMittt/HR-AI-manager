from __future__ import annotations

import html
import logging
from functools import wraps
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import storage
from config import (
    get_sessions_dir,
    get_summaries_dir,
    load_settings,
    save_settings,
)

logger = logging.getLogger(__name__)
router = Router()

STATE_LABELS = {
    "waiting_resume": "Ждёт резюме",
    "interviewing": "Собеседование",
    "blocked": "Пауза",
    "waiting_decision": "Ждёт решения",
    "completed": "Завершено",
}

STATE_EMOJI = {
    "waiting_resume": "⏳",
    "interviewing": "💬",
    "blocked": "🚫",
    "waiting_decision": "🕒",
    "completed": "✅",
}


DECISION_LABELS = {
    "pending": "Ожидает решения",
    "approved": "Одобрен",
    "rejected": "Отклонен",
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


def _effective_state(session: dict) -> str:
    raw_state = str(session.get("state", "waiting_resume") or "waiting_resume")
    decision = str(session.get("employer_decision", "pending") or "pending")
    if raw_state == "completed" and decision == "pending":
        return "waiting_decision"
    if raw_state == "waiting_decision" and decision in {"approved", "rejected"}:
        return "completed"
    return raw_state


def _log_employer(
    level: int,
    user_id: int,
    event: str,
    **fields,
):
    parts = [f"user_id={user_id}", f"event={event}"]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={_compact_log_value(value)}")
    logger.log(level, "employer %s", " ".join(parts))


def _compact_log_value(value) -> str:
    text = str(value).strip().replace("\r", " ").replace("\n", " ")
    text = " ".join(text.split())
    if len(text) > 80:
        return text[:77] + "..."
    return text or "-"


def is_employer(user_id: int) -> bool:
    settings = load_settings()
    return user_id in settings.get("employer_ids", [])


def employer_only(func):
    @wraps(func)
    async def wrapper(message: Message, *args, **kwargs):
        if not is_employer(message.from_user.id):
            _log_employer(
                logging.WARNING,
                message.from_user.id,
                "access_denied",
                target=func.__name__,
            )
            await message.answer("Нет доступа.")
            return
        return await func(message, *args, **kwargs)

    return wrapper


@router.message(Command("employer"))
@employer_only
async def cmd_employer(message: Message):
    _log_employer(
        logging.INFO,
        message.from_user.id,
        "menu_opened",
        source="telegram_command",
    )
    await message.answer(
        "<b>Панель работодателя</b>\n\nВыберите действие:",
        reply_markup=_menu_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "emp:sessions")
async def cb_sessions(callback: CallbackQuery):
    if not is_employer(callback.from_user.id):
        _log_employer(
            logging.WARNING,
            callback.from_user.id,
            "access_denied",
            target="sessions_callback",
        )
        await callback.answer("Нет доступа", show_alert=True)
        return

    sessions = storage.list_sessions()
    _log_employer(
        logging.INFO,
        callback.from_user.id,
        "sessions_opened",
        count=len(sessions),
    )
    if not sessions:
        await callback.message.edit_text(
            "Сессий пока нет.",
            reply_markup=_back_keyboard(),
        )
        await callback.answer()
        return

    buttons = []
    lines = [f"<b>Сессии кандидатов</b>\nВсего: {len(sessions)}\n"]
    for session in sessions[:12]:
        state = _effective_state(session)
        username = session["username"] or str(session["user_id"])
        lines.append(
            f"{STATE_EMOJI.get(state, '•')} "
            f"{html.escape(username)} "
            f"({STATE_LABELS.get(state, state)})"
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{STATE_EMOJI.get(state, '•')} {username}",
                    callback_data=f"emp:session:{session['user_id']}",
                )
            ]
        )

    buttons.append(
        [InlineKeyboardButton(text="Назад", callback_data="emp:menu")]
    )

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("emp:session:"))
async def cb_session_detail(callback: CallbackQuery):
    if not is_employer(callback.from_user.id):
        _log_employer(
            logging.WARNING,
            callback.from_user.id,
            "access_denied",
            target="session_detail_callback",
        )
        await callback.answer("Нет доступа", show_alert=True)
        return

    user_id = int(callback.data.split(":")[-1])
    session = storage.get_session_snapshot(user_id)
    if not session:
        await callback.answer("Сессия не найдена", show_alert=True)
        return
    _log_employer(
        logging.INFO,
        callback.from_user.id,
        "session_opened",
        candidate_user_id=user_id,
        state=session.get("state"),
    )

    history = session.get("interview_history", [])
    answers_count = sum(
        1
        for item in history
        if item.get("role") == "user" and item.get("source") == "candidate"
    )
    analysis_preview = _trim_text(session.get("resume_analysis") or "—", 800)
    vacancy = session.get("vacancy") or {}
    score = session.get("candidate_score") or {}
    decision = DECISION_LABELS.get(
        session.get("employer_decision", "pending"),
        session.get("employer_decision", "pending"),
    )
    state = _effective_state(session)

    lines = [
        f"<b>Сессия кандидата</b>",
        f"ID: <code>{session['user_id']}</code>",
        f"Вакансия: <b>{html.escape(vacancy.get('title', '—'))}</b>",
        f"Score: <b>{score.get('overall_score', '-')}</b>/40",
        f"Порог: <b>{score.get('threshold', vacancy.get('score_threshold', '-'))}</b>/40",
        f"Решение: <b>{html.escape(decision)}</b>",
        f"Имя: <b>{html.escape(session['username'] or '—')}</b>",
        f"Статус: <b>{STATE_LABELS.get(state, state)}</b>",
        f"Начало: {html.escape(_format_timestamp(session['started_at']))}",
        f"Обновлено: {html.escape(_format_timestamp(session['updated_at']))}",
        f"Resume: {'да' if session['resume_received'] else 'нет'}",
        f"Ответов в интервью: {answers_count}",
        f"Off-topic срабатываний: {session.get('off_topic_count', 0)}",
        "",
        "<b>Краткий анализ резюме</b>",
        html.escape(analysis_preview),
    ]

    buttons = []
    summary_path = session.get("summary_path")
    if summary_path and Path(summary_path).exists():
        buttons.append(
            [
                InlineKeyboardButton(
                    text="Скачать сводку",
                    callback_data=f"emp:session_summary:{session['user_id']}",
                )
            ]
        )

    buttons.append([InlineKeyboardButton(text="Назад", callback_data="emp:sessions")])

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("emp:decision:"))
async def cb_decision(callback: CallbackQuery):
    if not is_employer(callback.from_user.id):
        _log_employer(
            logging.WARNING,
            callback.from_user.id,
            "access_denied",
            target="decision_callback",
        )
        await callback.answer("Нет доступа", show_alert=True)
        return

    _, _, user_id_raw, decision = callback.data.split(":", 3)
    user_id = int(user_id_raw)
    if decision not in {"approved", "rejected", "pending"}:
        await callback.answer("Неизвестное решение", show_alert=True)
        return

    session = await storage.load_session(user_id)
    previous_decision = session.get("employer_decision", "pending")
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
    notification_text = _candidate_decision_message(decision)
    if notification_text and decision != previous_decision:
        try:
            await callback.bot.send_message(user_id, notification_text)
            storage.add_dialog_message(session, "assistant", notification_text, source="decision_update")
            storage.add_session_event(
                session,
                "employer",
                "candidate_notified_about_decision",
                decision=decision,
            )
        except Exception as error:
            _log_employer(
                logging.WARNING,
                callback.from_user.id,
                "candidate_decision_notify_failed",
                candidate_user_id=user_id,
                decision=decision,
                error=error,
            )
    await storage.save_session(session)
    _log_employer(
        logging.INFO,
        callback.from_user.id,
        "decision_changed",
        candidate_user_id=user_id,
        decision=decision,
    )
    await cb_session_detail(callback)


@router.callback_query(F.data == "emp:summaries")
async def cb_summaries(callback: CallbackQuery):
    if not is_employer(callback.from_user.id):
        _log_employer(
            logging.WARNING,
            callback.from_user.id,
            "access_denied",
            target="summaries_callback",
        )
        await callback.answer("Нет доступа", show_alert=True)
        return

    summaries = storage.list_summaries()
    _log_employer(
        logging.INFO,
        callback.from_user.id,
        "summaries_opened",
        count=len(summaries),
    )
    if not summaries:
        await callback.message.edit_text(
            "Сводок пока нет. Они появятся после завершения собеседований.",
            reply_markup=_back_keyboard(),
        )
        await callback.answer()
        return

    buttons = []
    for index, path in enumerate(summaries[:10]):
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"Файл {index + 1}: {path.stem[:35]}",
                    callback_data=f"emp:summary_idx:{index}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text="Назад", callback_data="emp:menu")])

    await callback.message.edit_text(
        (
            f"<b>Сводки кандидатов</b>\n"
            f"Папка: <code>{html.escape(str(get_summaries_dir()))}</code>\n"
            f"Всего файлов: {len(summaries)}"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("emp:summary_idx:"))
async def cb_get_summary(callback: CallbackQuery):
    if not is_employer(callback.from_user.id):
        _log_employer(
            logging.WARNING,
            callback.from_user.id,
            "access_denied",
            target="summary_download_callback",
        )
        await callback.answer("Нет доступа", show_alert=True)
        return

    index = int(callback.data.split(":")[-1])
    summaries = storage.list_summaries()
    if index >= len(summaries):
        await callback.answer("Файл не найден", show_alert=True)
        return

    _log_employer(
        logging.INFO,
        callback.from_user.id,
        "summary_downloaded",
        source="list",
        file_name=summaries[index].name,
    )
    await _send_summary_file(callback.message, summaries[index])
    await callback.answer()


@router.callback_query(F.data.startswith("emp:session_summary:"))
async def cb_get_session_summary(callback: CallbackQuery):
    if not is_employer(callback.from_user.id):
        _log_employer(
            logging.WARNING,
            callback.from_user.id,
            "access_denied",
            target="session_summary_download_callback",
        )
        await callback.answer("Нет доступа", show_alert=True)
        return

    user_id = int(callback.data.split(":")[-1])
    session = storage.get_session_snapshot(user_id)
    if not session or not session.get("summary_path"):
        await callback.answer("Сводка недоступна", show_alert=True)
        return

    path = Path(session["summary_path"])
    if not path.exists():
        await callback.answer("Файл не найден", show_alert=True)
        return

    _log_employer(
        logging.INFO,
        callback.from_user.id,
        "summary_downloaded",
        source="session",
        candidate_user_id=user_id,
        file_name=path.name,
    )
    await _send_summary_file(callback.message, path)
    await callback.answer()


@router.callback_query(F.data == "emp:settings")
async def cb_settings(callback: CallbackQuery):
    if not is_employer(callback.from_user.id):
        _log_employer(
            logging.WARNING,
            callback.from_user.id,
            "access_denied",
            target="settings_callback",
        )
        await callback.answer("Нет доступа", show_alert=True)
        return

    settings = load_settings()
    _log_employer(
        logging.INFO,
        callback.from_user.id,
        "settings_opened",
        model=settings.get("openrouter_model"),
    )
    free_models = settings.get("openrouter_free_models", [])
    free_models_preview = ", ".join(free_models[:3]) if free_models else "—"
    model_label = (
        "auto (перебор бесплатных моделей)"
        if settings["openrouter_model"].strip().lower() == "auto"
        else settings["openrouter_model"]
    )

    text = (
        "<b>Текущие настройки</b>\n\n"
        f"Модель: <code>{html.escape(model_label)}</code>\n"
        f"Free fallback: {html.escape(free_models_preview)}\n"
        f"Блокировка: <b>{settings['block_duration_seconds']} сек.</b>\n"
        f"Вопросов: <b>{settings['interview_questions_count']}</b>\n"
        f"Папка сессий: <code>{html.escape(str(get_sessions_dir(settings)))}</code>\n"
        f"Папка summary: <code>{html.escape(str(get_summaries_dir(settings)))}</code>\n"
        f"Вакансия: <b>{html.escape(settings['vacancy']['title'])}</b>\n\n"
        "Команды:\n"
        "/set_block 30\n"
        "/set_questions 7\n"
        "/set_model auto\n"
        "/set_model mistralai/mistral-7b-instruct:free\n"
        "/set_free_models model1, model2\n"
        "/set_summary_dir sessions\\summaries"
    )
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_back_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "emp:vacancy")
async def cb_vacancy(callback: CallbackQuery):
    if not is_employer(callback.from_user.id):
        _log_employer(
            logging.WARNING,
            callback.from_user.id,
            "access_denied",
            target="vacancy_callback",
        )
        await callback.answer("Нет доступа", show_alert=True)
        return

    settings = load_settings()
    _log_employer(
        logging.INFO,
        callback.from_user.id,
        "vacancy_opened",
        title=settings["vacancy"]["title"],
    )
    vacancy = settings["vacancy"]
    text = (
        "<b>Текущая вакансия</b>\n\n"
        f"Название: <b>{html.escape(vacancy['title'])}</b>\n"
        f"Описание: {html.escape(vacancy['description'])}\n"
        f"Навыки: {html.escape(', '.join(vacancy['required_skills']))}\n\n"
        "Команды:\n"
        "/set_vacancy_title Python Developer\n"
        "/set_vacancy_desc Описание вакансии\n"
        "/set_vacancy_skills Python, SQL, Django"
    )
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_back_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "emp:menu")
async def cb_menu(callback: CallbackQuery):
    if not is_employer(callback.from_user.id):
        _log_employer(
            logging.WARNING,
            callback.from_user.id,
            "access_denied",
            target="menu_callback",
        )
        await callback.answer("Нет доступа", show_alert=True)
        return

    _log_employer(
        logging.INFO,
        callback.from_user.id,
        "menu_opened",
        source="callback",
    )
    await callback.message.edit_text(
        "<b>Панель работодателя</b>\n\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=_menu_keyboard(),
    )
    await callback.answer()


@router.message(Command("set_block"))
@employer_only
async def cmd_set_block(message: Message):
    try:
        seconds = int(message.text.split(maxsplit=1)[1])
        if seconds < 1:
            raise ValueError
    except (IndexError, ValueError):
        await message.answer("Использование: /set_block 30")
        return

    settings = load_settings()
    settings["block_duration_seconds"] = seconds
    save_settings(settings)
    _log_employer(
        logging.INFO,
        message.from_user.id,
        "setting_updated",
        name="block_duration_seconds",
        value=seconds,
    )
    await message.answer(
        f"Время блокировки обновлено: <b>{seconds} сек.</b>",
        parse_mode="HTML",
    )


@router.message(Command("set_questions"))
@employer_only
async def cmd_set_questions(message: Message):
    try:
        count = int(message.text.split(maxsplit=1)[1])
        if count < 1:
            raise ValueError
    except (IndexError, ValueError):
        await message.answer("Использование: /set_questions 7")
        return

    settings = load_settings()
    settings["interview_questions_count"] = count
    save_settings(settings)
    _log_employer(
        logging.INFO,
        message.from_user.id,
        "setting_updated",
        name="interview_questions_count",
        value=count,
    )
    await message.answer(
        f"Количество вопросов обновлено: <b>{count}</b>",
        parse_mode="HTML",
    )


@router.message(Command("set_model"))
@employer_only
async def cmd_set_model(message: Message):
    try:
        model = message.text.split(maxsplit=1)[1].strip()
    except IndexError:
        await message.answer(
            "Использование: /set_model auto или /set_model mistralai/mistral-7b-instruct:free"
        )
        return

    settings = load_settings()
    settings["openrouter_model"] = model
    save_settings(settings)
    _log_employer(
        logging.INFO,
        message.from_user.id,
        "setting_updated",
        name="openrouter_model",
        value=model,
    )

    if model.lower() == "auto":
        text = "Режим модели: <b>auto</b>. Бот будет перебирать бесплатные модели из списка fallback."
    else:
        text = f"Модель OpenRouter обновлена: <code>{html.escape(model)}</code>"
    await message.answer(text, parse_mode="HTML")


@router.message(Command("set_free_models"))
@employer_only
async def cmd_set_free_models(message: Message):
    try:
        raw = message.text.split(maxsplit=1)[1].strip()
    except IndexError:
        await message.answer(
            "Использование: /set_free_models model1, model2, model3"
        )
        return

    models = [item.strip() for item in raw.split(",") if item.strip()]
    if not models:
        await message.answer("Нужен хотя бы один id модели.")
        return

    settings = load_settings()
    settings["openrouter_free_models"] = models
    save_settings(settings)
    _log_employer(
        logging.INFO,
        message.from_user.id,
        "setting_updated",
        name="openrouter_free_models",
        models_count=len(models),
    )
    await message.answer(
        "Список fallback-моделей обновлён:\n" + "\n".join(models)
    )


@router.message(Command("set_summary_dir"))
@employer_only
async def cmd_set_summary_dir(message: Message):
    try:
        raw_path = message.text.split(maxsplit=1)[1].strip()
    except IndexError:
        await message.answer("Использование: /set_summary_dir sessions\\summaries")
        return

    settings = load_settings()
    settings["summaries_dir"] = raw_path
    save_settings(settings)
    _log_employer(
        logging.INFO,
        message.from_user.id,
        "setting_updated",
        name="summaries_dir",
        value=get_summaries_dir(settings),
    )
    await message.answer(
        f"Папка для summary обновлена: <code>{html.escape(str(get_summaries_dir(settings)))}</code>",
        parse_mode="HTML",
    )


@router.message(Command("set_vacancy_title"))
@employer_only
async def cmd_set_vacancy_title(message: Message):
    try:
        title = message.text.split(maxsplit=1)[1].strip()
    except IndexError:
        await message.answer("Использование: /set_vacancy_title Python Developer")
        return

    settings = load_settings()
    settings["vacancy"]["title"] = title
    save_settings(settings)
    _log_employer(
        logging.INFO,
        message.from_user.id,
        "vacancy_updated",
        field="title",
        value=title,
    )
    await message.answer(
        f"Название вакансии обновлено: <b>{html.escape(title)}</b>",
        parse_mode="HTML",
    )


@router.message(Command("set_vacancy_desc"))
@employer_only
async def cmd_set_vacancy_desc(message: Message):
    try:
        description = message.text.split(maxsplit=1)[1].strip()
    except IndexError:
        await message.answer("Использование: /set_vacancy_desc Описание вакансии")
        return

    settings = load_settings()
    settings["vacancy"]["description"] = description
    save_settings(settings)
    _log_employer(
        logging.INFO,
        message.from_user.id,
        "vacancy_updated",
        field="description",
        chars=len(description),
    )
    await message.answer("Описание вакансии обновлено.")


@router.message(Command("set_vacancy_skills"))
@employer_only
async def cmd_set_vacancy_skills(message: Message):
    try:
        raw = message.text.split(maxsplit=1)[1].strip()
    except IndexError:
        await message.answer("Использование: /set_vacancy_skills Python, SQL, Git")
        return

    skills = [item.strip() for item in raw.split(",") if item.strip()]
    if not skills:
        await message.answer("Нужно указать хотя бы один навык.")
        return

    settings = load_settings()
    settings["vacancy"]["required_skills"] = skills
    save_settings(settings)
    _log_employer(
        logging.INFO,
        message.from_user.id,
        "vacancy_updated",
        field="required_skills",
        count=len(skills),
    )
    await message.answer(
        f"Навыки обновлены: <b>{html.escape(', '.join(skills))}</b>",
        parse_mode="HTML",
    )


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Список сессий",
                    callback_data="emp:sessions",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Сводки кандидатов",
                    callback_data="emp:summaries",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Настройки",
                    callback_data="emp:settings",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Вакансия",
                    callback_data="emp:vacancy",
                )
            ],
        ]
    )


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data="emp:menu")]
        ]
    )


def _trim_text(value: str, limit: int) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _format_timestamp(raw_value: str) -> str:
    return raw_value[:19].replace("T", " ") if raw_value else "—"


async def _send_summary_file(message: Message, path: Path):
    await message.answer_document(
        FSInputFile(path, filename=path.name),
        caption=f"Сводка: {path.name}",
    )
