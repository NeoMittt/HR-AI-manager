from __future__ import annotations

import asyncio
import io
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import PyPDF2
from aiogram import Bot, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import ai_client
import storage
from config import get_vacancy_snapshot, list_open_vacancies, load_settings

logger = logging.getLogger(__name__)
router = Router()


def get_msg(key: str, **kwargs) -> str:
    settings = load_settings()
    template = settings["messages"].get(key, "")
    return template.format(**kwargs) if kwargs else template


@asynccontextmanager
async def _typing_indicator(message: Message, *, interval_seconds: float = 4.0):
    stop_event = asyncio.Event()

    async def _worker():
        while not stop_event.is_set():
            try:
                await message.bot.send_chat_action(message.chat.id, "typing")
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                continue

    task = asyncio.create_task(_worker())
    try:
        yield
    finally:
        stop_event.set()
        try:
            await task
        except Exception:
            pass


async def _answer_with_typing(message: Message, text: str, **kwargs):
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass
    return await message.answer(text, **kwargs)


def _log_candidate(level: int, user_id: int, event: str, **fields):
    parts = [f"user_id={user_id}", f"event={event}"]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={_compact_log_value(value)}")
    logger.log(level, "candidate %s", " ".join(parts))


def _compact_log_value(value) -> str:
    text = str(value).strip().replace("\r", " ").replace("\n", " ")
    text = " ".join(text.split())
    if len(text) > 80:
        return text[:77] + "..."
    return text or "-"


def _history_count(session: dict, role: str) -> int:
    return sum(
        1
        for item in _current_round_history(session)
        if item.get("role") == role
    )


def _interview_answer_count(session: dict) -> int:
    return sum(
        1
        for item in _current_round_history(session)
        if item.get("role") == "user" and item.get("source") == "candidate"
    )


def _ai_question_count(session: dict) -> int:
    return sum(
        1
        for item in _current_round_history(session)
        if item.get("role") == "assistant" and item.get("source") in {"ai", "ai_question"}
    )


def _current_round_history(session: dict) -> list[dict]:
    history = session.get("interview_history", [])
    try:
        start = int(session.get("round_history_start_index", 0) or 0)
    except (TypeError, ValueError):
        start = 0
    if start < 0 or start > len(history):
        start = 0
    return history[start:]


def _session_vacancy(session: dict, settings: dict | None = None) -> dict:
    settings = settings or load_settings()
    current = session.get("vacancy")
    if isinstance(current, dict) and current.get("title"):
        current.setdefault("required_skills", [])
        current.setdefault("score_threshold", 28)
        current.setdefault("key", session.get("vacancy_key"))
        return current

    vacancy_key = session.get("vacancy_key")
    vacancy = get_vacancy_snapshot(settings, vacancy_key)
    session["vacancy_key"] = vacancy.get("key")
    session["vacancy"] = vacancy
    return vacancy


def _record_assistant_message(session: dict, text: str, *, source: str):
    if not text:
        return
    storage.add_dialog_message(session, "assistant", text, source=source)


def _record_candidate_message(session: dict, text: str, *, source: str):
    if not text:
        return
    storage.add_dialog_message(session, "user", text, source=source)


def _message_history_text(message: Message) -> str:
    if message.text and message.text.strip():
        return message.text.strip()
    if message.document:
        file_name = message.document.file_name or "file"
        if message.document.mime_type == "application/pdf":
            return f"[Кандидат отправил PDF: {file_name}]"
        return f"[Кандидат отправил файл: {file_name}]"
    if message.caption and message.caption.strip():
        return message.caption.strip()
    return "[Кандидат отправил неподдерживаемое сообщение]"


def _has_selected_vacancy(session: dict) -> bool:
    vacancy = session.get("vacancy")
    return bool(session.get("vacancy_key") and isinstance(vacancy, dict) and vacancy.get("title"))


def _has_previous_application(session: dict) -> bool:
    return bool(
        session.get("resume_text")
        or session.get("summary_saved")
        or session.get("resume_analysis")
        or session.get("candidate_score")
        or session.get("session_events")
        or session.get("interview_history")
    )


def _vacancy_choice_keyboard(settings: dict | None = None) -> InlineKeyboardMarkup:
    current = settings or load_settings()
    rows = []
    for vacancy in list_open_vacancies(current):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{vacancy.get('label', vacancy.get('title', 'Вакансия'))}: {vacancy.get('title', '')}",
                    callback_data=f"cand:vacancy:{vacancy['key']}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _repeat_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Обновить резюме", callback_data="cand:repeat:update")],
            [InlineKeyboardButton(text="Выбрать другую вакансию", callback_data="cand:repeat:switch")],
            [InlineKeyboardButton(text="Показать текущий статус", callback_data="cand:repeat:continue")],
        ]
    )


def _decision_keyboard(user_id: int, *, include_summary: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Открыть сессию", callback_data=f"emp:session:{user_id}")],
        [
            InlineKeyboardButton(text="Одобрить", callback_data=f"emp:decision:{user_id}:approved"),
            InlineKeyboardButton(text="Отклонить", callback_data=f"emp:decision:{user_id}:rejected"),
        ],
    ]
    if include_summary:
        rows.append(
            [InlineKeyboardButton(text="Скачать summary", callback_data=f"emp:session_summary:{user_id}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _start_new_round(session: dict, *, keep_vacancy: bool):
    if session.get("interview_history"):
        storage.add_dialog_message(
            session,
            "assistant",
            "Начата новая попытка кандидата.",
            source="system_round",
        )
    session["round_number"] = max(1, int(session.get("round_number", 1) or 1) + 1)
    session["round_history_start_index"] = len(session.get("interview_history", []))
    session["state"] = "waiting_resume"
    session["return_state"] = None
    session["resume_text"] = None
    session["resume_analysis"] = None
    session["resume_screening"] = None
    session["candidate_score"] = None
    session["interview_notes"] = []
    session["summary_saved"] = False
    session["summary_path"] = None
    session["off_topic_count"] = 0
    session["block_until"] = None
    session["permanent_block"] = False
    session["block_reason"] = None
    session["employer_decision"] = "pending"
    session["awaiting_repeat_choice"] = False
    session["awaiting_vacancy_choice"] = not keep_vacancy
    if not keep_vacancy:
        session["vacancy_key"] = None
        session["vacancy"] = None
    _init_interview_script(session)


def _init_interview_script(session: dict):
    session["interview_topic_index"] = 0
    session["interview_followup_used"] = False
    session["interview_notes"] = []
    session["interview_topics"] = []
    session["last_question_text"] = None
    session["last_question_topic"] = None
    session["last_question_mode"] = None


def _interview_topics(session: dict) -> list[dict]:
    cached_topics = session.get("interview_topics")
    if isinstance(cached_topics, list) and cached_topics:
        return cached_topics

    settings = load_settings()
    vacancy = _session_vacancy(session, settings)
    screening = session.get("resume_screening") or {}
    total_questions = max(1, int(settings.get("interview_questions_count", 5)))
    gap_items = _collect_interview_gap_items(session, vacancy, screening)
    topics: list[dict] = []
    seen_keys: set[str] = set()
    for index, item in enumerate(gap_items, start=1):
        topic = _topic_from_gap(item, index=index, vacancy=vacancy)
        topic_key = str(topic.get("key") or topic.get("id") or "").strip()
        if not topic_key or topic_key in seen_keys:
            continue
        seen_keys.add(topic_key)
        topics.append(topic)
        if len(topics) >= total_questions:
            break

    session["interview_topics"] = topics
    return topics


def _collect_interview_gap_items(
    session: dict,
    vacancy: dict,
    screening: dict,
) -> list[str]:
    resume_text = session.get("resume_text") or ""
    total_questions = max(1, int(load_settings().get("interview_questions_count", 5)))
    items: list[str] = []
    items.extend(screening.get("missing_information") or [])
    items.extend(screening.get("key_gaps") or [])
    items.extend(_local_resume_gaps(resume_text, vacancy))

    result: list[str] = []
    seen: set[str] = set()
    for raw_item in items:
        item = " ".join(str(raw_item or "").split()).strip(" -")
        if not item:
            continue
        if _gap_already_covered_by_resume(item, resume_text):
            continue
        lowered = item.lower()
        if any(
            marker in lowered
            for marker in ("не совпадает с вакансией", "другая профессия", "нерелевант")
        ):
            continue
        key = _normalized_text(item)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)

    if result:
        return result[:total_questions]

    return _fallback_interview_gap_items(resume_text, vacancy)[:total_questions]


def _local_resume_gaps(resume_text: str, vacancy: dict) -> list[str]:
    lower = resume_text.lower()
    skills = vacancy.get("required_skills", [])
    items: list[str] = []

    if not _resume_mentions_project_details(lower):
        items.append("Конкретный пример последнего релевантного проекта и личной роли")
    if not _resume_mentions_salary(lower):
        items.append("Зарплатные ожидания")
    if not _resume_mentions_format(lower):
        items.append("Предпочитаемый формат работы")
    if not _resume_mentions_start_time(lower):
        items.append("Срок выхода на работу")

    for skill in skills[:3]:
        skill_text = str(skill).strip()
        if not skill_text:
            continue
        if skill_text.lower() not in lower:
            items.append(f"Подтвержденный опыт с {skill_text}")

    return items


def _fallback_interview_gap_items(resume_text: str, vacancy: dict) -> list[str]:
    lower = resume_text.lower()
    items = []
    if _resume_mentions_project_details(lower):
        items.append("Мотивация по вакансии")
    else:
        items.append("Конкретный пример последнего релевантного проекта и личной роли")
    if vacancy.get("required_skills"):
        items.append("Подтвержденный опыт с ключевыми инструментами вакансии")
    if not _resume_mentions_format(lower):
        items.append("Предпочитаемый формат работы")
    if not _resume_mentions_start_time(lower):
        items.append("Срок выхода на работу")
    return items


def _gap_already_covered_by_resume(item: str, resume_text: str) -> bool:
    lowered = item.lower()
    lower_resume_text = resume_text.lower()
    if any(fragment in lowered for fragment in ("зарплат", "оплат", "salary", "оклад")):
        return _resume_mentions_salary(lower_resume_text)
    if any(fragment in lowered for fragment in ("формат", "удален", "удалён", "гибрид", "офис")):
        return _resume_mentions_format(lower_resume_text)
    if any(fragment in lowered for fragment in ("срок", "выход", "notice", "готов выйти")):
        return _resume_mentions_start_time(lower_resume_text)
    return False


def _resume_mentions_project_details(lower_resume_text: str) -> bool:
    return any(
        fragment in lower_resume_text
        for fragment in (
            "проект",
            "проекты",
            "задач",
            "результат",
            "достижен",
            "роль",
            "достижения",
        )
    )


def _resume_mentions_salary(lower_resume_text: str) -> bool:
    if any(
        fragment in lower_resume_text
        for fragment in ("зарплат", "salary", "оклад", "ожидан", "доход", "компенсац", "зп")
    ):
        return True
    money_pattern = re.compile(
        r"\b\d[\d\s]{2,}(?:\s*(?:₸|тенге|руб(?:лей|\.|)?|₽|usd|eur|\$|€))",
        flags=re.IGNORECASE,
    )
    return bool(money_pattern.search(lower_resume_text))


def _resume_mentions_format(lower_resume_text: str) -> bool:
    return any(
        fragment in lower_resume_text
        for fragment in (
            "удален",
            "удалён",
            "гибрид",
            "офис",
            "формат работы",
            "remote",
            "hybrid",
            "onsite",
        )
    )


def _resume_mentions_start_time(lower_resume_text: str) -> bool:
    return any(
        fragment in lower_resume_text
        for fragment in (
            "срок выхода",
            "готов выйти",
            "смогу выйти",
            "notice",
            "дата выхода",
            "через",
            "после оффера",
            "can start",
            "available from",
            "available in",
            "start in",
        )
    )


def _topic_from_gap(item: str, *, index: int, vacancy: dict) -> dict:
    gap_label = " ".join(str(item or "").split())
    lowered = gap_label.lower()
    required_skills = [str(skill).strip() for skill in vacancy.get("required_skills", []) if str(skill).strip()]
    skills_text = ", ".join(required_skills[:4]) or "ключевой стек вакансии"
    matched_skill = next(
        (skill for skill in required_skills if skill.lower() in lowered),
        None,
    )

    if any(fragment in lowered for fragment in ("зарплат", "оплат", "salary", "оклад")):
        return {
            "id": "salary_expectations",
            "key": "salary_expectations",
            "name": "зарплатные ожидания",
            "goal": "уточнить ожидания по компенсации",
            "question": "Подскажите, пожалуйста, какие у вас ожидания по зарплате для этой вакансии?",
            "follow_up": "Уточните, пожалуйста, желаемую сумму или диапазон и насколько это гибкое ожидание.",
            "gap_label": gap_label,
        }

    if any(fragment in lowered for fragment in ("формат", "удален", "удалён", "гибрид", "офис")):
        return {
            "id": "work_format",
            "key": "work_format",
            "name": "формат работы",
            "goal": "уточнить предпочтительный формат работы",
            "question": "Какой формат работы для вас предпочтителен: удаленно, гибридно или офис?",
            "follow_up": "Если можно, уточните, какой формат для вас приоритетный и что для вас критично по графику.",
            "gap_label": gap_label,
        }

    if any(fragment in lowered for fragment in ("срок", "выход", "готов выйти", "start", "notice")):
        return {
            "id": "start_date",
            "key": "start_date",
            "name": "срок выхода",
            "goal": "понять, когда кандидат сможет приступить к работе",
            "question": "Когда вы сможете выйти на работу, если мы договоримся о следующем этапе?",
            "follow_up": "Уточните, пожалуйста, нужна ли вам отработка или вы сможете начать быстрее.",
            "gap_label": gap_label,
        }

    if any(fragment in lowered for fragment in ("мотива", "интерес", "почему вам", "ваканси")):
        return {
            "id": "motivation",
            "key": "motivation",
            "name": "мотивация",
            "goal": "понять, почему кандидату интересна именно эта вакансия",
            "question": "Почему вам интересна именно эта вакансия и что для вас важно в будущих задачах?",
            "follow_up": "Если можно, уточните, что именно в этой роли вам откликается сильнее всего.",
            "gap_label": gap_label,
        }

    if matched_skill:
        skill_key = f"skill:{_normalized_text(matched_skill)}"
        return {
            "id": skill_key,
            "key": skill_key,
            "name": f"опыт с {matched_skill}",
            "goal": f"подтвердить практический опыт кандидата с {matched_skill}",
            "question": (
                f"В резюме не хватило деталей по опыту с {matched_skill}. "
                f"Расскажите, пожалуйста, что именно вы делали с этим инструментом на практике."
            ),
            "follow_up": "Уточните, пожалуйста, в каком проекте это было, что делали лично вы и какой получили результат.",
            "gap_label": gap_label,
        }

    if any(
        fragment in lowered
        for fragment in ("проект", "кейс", "роль", "результат", "задач", "опыт", "инструмент")
    ):
        return {
            "id": "project_example",
            "key": "project_example",
            "name": "пример из опыта",
            "goal": "получить конкретный пример реальной задачи из опыта кандидата",
            "question": (
                "Расскажите, пожалуйста, об одном последнем релевантном проекте или рабочей задаче: "
                "какая была цель, что делали лично вы и какой получили результат?"
            ),
            "follow_up": "Если можно, добавьте, пожалуйста, больше конкретики: стек, личная зона ответственности и итог.",
            "gap_label": gap_label,
        }

    generic_key = f"gap:{_normalized_text(gap_label) or index}"
    return {
        "id": generic_key,
        "key": generic_key,
        "name": gap_label,
        "goal": f"уточнить недостающую информацию по пункту: {gap_label}",
        "question": (
            f"В резюме не хватило информации по пункту «{gap_label}». "
            "Расскажите, пожалуйста, об этом подробнее."
        ),
        "follow_up": (
            f"Уточните, пожалуйста, пункт «{gap_label}»: конкретный пример, ваша роль, "
            f"использованные инструменты и результат. Если это связано со стеком, ориентируйтесь на {skills_text}."
        ),
        "gap_label": gap_label,
    }


def _current_interview_topic(session: dict) -> dict | None:
    topics = _interview_topics(session)
    index = int(session.get("interview_topic_index", 0))
    if index < 0 or index >= len(topics):
        return None
    return topics[index]


def _normalized_text(value: str) -> str:
    return " ".join(re.findall(r"[\w#+.-]+", (value or "").lower(), flags=re.UNICODE))


def _enforce_profession_gate(
    screening: dict,
    *,
    vacancy: dict,
    resume_text: str,
) -> dict:
    result = dict(screening or {})
    vacancy_text = _normalized_text(
        " ".join(
            [
                vacancy.get("title", ""),
                vacancy.get("description", ""),
                " ".join(vacancy.get("required_skills", [])),
            ]
        )
    )
    resume_normalized = _normalized_text(resume_text)

    if result.get("should_reject") or not result.get("profession_match", True):
        result["profession_match"] = False
        result["should_reject"] = True
        return result

    developer_markers = (
        "python",
        "developer",
        "backend",
        "frontend",
        "fullstack",
        "fastapi",
        "django",
        "flask",
        "sql",
        "rest",
        "api",
        "git",
        "програм",
        "разработ",
        "бэкенд",
        "фронтенд",
    )
    mismatch_markers = (
        "продав",
        "кассир",
        "бухгалтер",
        "юрист",
        "маркетолог",
        "smm",
        "дизайнер",
        "рекрутер",
        "hr ",
        "водител",
        "бариста",
        "официант",
        "повар",
        "учитель",
        "педагог",
        "врач",
        "медсест",
        "оператор call",
        "колл центр",
        "call center",
    )

    vacancy_is_developer = any(marker in vacancy_text for marker in developer_markers)
    target_hits = sum(1 for marker in developer_markers if marker in resume_normalized)
    mismatch_hits = sum(1 for marker in mismatch_markers if marker in resume_normalized)

    if vacancy_is_developer and target_hits == 0 and mismatch_hits >= 2:
        result["profession_match"] = False
        result["should_reject"] = True
        result["fit_score"] = min(int(result.get("fit_score") or 2), 2)
        result["candidate_message"] = (
            result.get("candidate_message")
            or "Спасибо за отклик. Сейчас мы не продолжаем интервью, потому что профиль резюме не совпадает с вакансией."
        )
        employer_summary = str(result.get("employer_summary") or "").strip()
        mismatch_note = (
            "Профессия кандидата выглядит нерелевантной для этой вакансии: в резюме нет признаков "
            "разработки, но есть явные маркеры другой профессии."
        )
        result["employer_summary"] = (
            f"{employer_summary}\n\n{mismatch_note}".strip()
            if employer_summary
            else mismatch_note
        )
        key_gaps = list(result.get("key_gaps") or [])
        if "Профессия кандидата не совпадает с вакансией" not in key_gaps:
            key_gaps.insert(0, "Профессия кандидата не совпадает с вакансией")
        result["key_gaps"] = key_gaps[:8]

    return result


def _build_script_question_text(
    session: dict,
    topic: dict,
    *,
    follow_up: bool,
    assessment: dict | None = None,
) -> str:
    if not follow_up:
        if int(session.get("interview_topic_index", 0)) == 0:
            return topic["question"]
        return f"Спасибо, понял. {topic['question']}"

    suggested = ""
    if assessment:
        suggested = str(assessment.get("suggested_follow_up_question", "")).strip()
    if suggested:
        return suggested

    missing_points = []
    if assessment:
        missing_points = list(assessment.get("missing_points") or [])
    if missing_points:
        return (
            f"{topic['follow_up']} "
            f"Если можно, уточните: {', '.join(missing_points[:2]).lower()}."
        )
    return topic["follow_up"]


async def _send_script_question(
    message: Message,
    session: dict,
    *,
    follow_up: bool,
    assessment: dict | None = None,
):
    topic = _current_interview_topic(session)
    if topic is None:
        await _finish_interview(message, session)
        return

    question_text = _build_script_question_text(
        session,
        topic,
        follow_up=follow_up,
        assessment=assessment,
    )
    source = "ai_followup" if follow_up else "ai_question"
    storage.add_dialog_message(session, "assistant", question_text, source=source)
    session["last_question_text"] = question_text
    session["last_question_topic"] = topic["id"]
    session["last_question_mode"] = "follow_up" if follow_up else "question"
    session["interview_followup_used"] = follow_up
    storage.add_session_event(
        session,
        "system",
        "interview_reply_ready",
        question_index=int(session.get("interview_topic_index", 0)) + 1,
        topic=topic["id"],
        follow_up=follow_up,
        chars=len(question_text),
        completed=False,
    )
    await storage.save_session(session)
    _log_candidate(
        logging.INFO,
        session["user_id"],
        "interview_reply_ready",
        question_index=int(session.get("interview_topic_index", 0)) + 1,
        topic=topic["id"],
        follow_up=follow_up,
        chars=len(question_text),
        completed=False,
    )
    await _answer_with_typing(message, question_text)


@router.message(CommandStart())
async def cmd_start(message: Message):
    if message.chat.type != "private":
        return

    user_id = message.from_user.id
    is_new_session = not storage.session_exists(user_id)
    session = await storage.load_session(user_id)
    session["username"] = _candidate_name(message)
    settings = load_settings()

    if is_new_session:
        session["awaiting_vacancy_choice"] = True
        storage.add_session_event(
            session,
            "candidate",
            "session_started",
            source="command_start",
        )
        greeting_text = get_msg("vacancy_choice")
        _record_assistant_message(session, greeting_text, source="greeting")
        await storage.save_session(session)
        _log_candidate(
            logging.INFO,
            user_id,
            "session_started",
            source="command_start",
            username=session["username"],
        )
        await message.answer(
            greeting_text,
            parse_mode="HTML",
            reply_markup=_vacancy_choice_keyboard(settings),
        )
        return

    if _has_previous_application(session):
        session["awaiting_repeat_choice"] = True
        vacancy = _session_vacancy(session, settings) if _has_selected_vacancy(session) else get_vacancy_snapshot(settings)
        repeat_text = get_msg("repeat_options", title=vacancy["title"])
        _record_assistant_message(session, repeat_text, source="repeat_prompt")
        storage.add_session_event(session, "candidate", "repeat_prompt_opened")
        await storage.save_session(session)
        await message.answer(
            repeat_text,
            parse_mode="HTML",
            reply_markup=_repeat_keyboard(),
        )
        return

    session["awaiting_vacancy_choice"] = True
    greeting_text = get_msg("vacancy_choice")
    _record_assistant_message(session, greeting_text, source="greeting")
    await storage.save_session(session)
    await message.answer(
        greeting_text,
        parse_mode="HTML",
        reply_markup=_vacancy_choice_keyboard(settings),
    )


@router.callback_query(lambda callback: callback.data and callback.data.startswith("cand:vacancy:"))
async def cb_candidate_vacancy(callback: CallbackQuery):
    if callback.message.chat.type != "private":
        await callback.answer()
        return

    user_id = callback.from_user.id
    session = await storage.load_session(user_id)
    session["username"] = callback.from_user.username or callback.from_user.full_name
    settings = load_settings()
    vacancy_key = callback.data.split(":")[-1]
    vacancy = get_vacancy_snapshot(settings, vacancy_key)
    session["vacancy_key"] = vacancy["key"]
    session["vacancy"] = vacancy
    session["awaiting_vacancy_choice"] = False
    session["awaiting_repeat_choice"] = False
    storage.add_session_event(
        session,
        "candidate",
        "vacancy_selected",
        vacancy_key=vacancy["key"],
        vacancy_title=vacancy["title"],
    )
    reply_text = get_msg("vacancy_selected", title=vacancy["title"])
    _record_assistant_message(session, reply_text, source="vacancy_selected")
    await storage.save_session(session)
    await callback.message.edit_text(reply_text, parse_mode="HTML")
    await callback.answer("Вакансия выбрана")


@router.callback_query(lambda callback: callback.data and callback.data.startswith("cand:repeat:"))
async def cb_candidate_repeat(callback: CallbackQuery):
    if callback.message.chat.type != "private":
        await callback.answer()
        return

    user_id = callback.from_user.id
    action = callback.data.split(":")[-1]
    session = await storage.load_session(user_id)
    session["username"] = callback.from_user.username or callback.from_user.full_name
    settings = load_settings()

    if action == "update":
        keep_vacancy = _has_selected_vacancy(session)
        if not keep_vacancy:
            session["awaiting_vacancy_choice"] = True
            session["awaiting_repeat_choice"] = False
            text = get_msg("repeat_choose_vacancy")
            _record_assistant_message(session, text, source="repeat_prompt")
            await storage.save_session(session)
            await callback.message.edit_text(
                text,
                reply_markup=_vacancy_choice_keyboard(settings),
            )
            await callback.answer("Выберите вакансию")
            return

        _start_new_round(session, keep_vacancy=True)
        vacancy = _session_vacancy(session, settings)
        storage.add_session_event(
            session,
            "candidate",
            "application_restarted",
            mode="same_vacancy",
            vacancy_key=vacancy.get("key"),
        )
        text = get_msg("repeat_resume_requested", title=vacancy["title"])
        _record_assistant_message(session, text, source="repeat_restart")
        await storage.save_session(session)
        await callback.message.edit_text(text, parse_mode="HTML")
        await callback.answer("Жду обновленное резюме")
        return

    if action == "switch":
        _start_new_round(session, keep_vacancy=False)
        storage.add_session_event(
            session,
            "candidate",
            "application_restarted",
            mode="switch_vacancy",
        )
        text = get_msg("repeat_choose_vacancy")
        _record_assistant_message(session, text, source="repeat_prompt")
        await storage.save_session(session)
        await callback.message.edit_text(
            text,
            reply_markup=_vacancy_choice_keyboard(settings),
        )
        await callback.answer("Выберите вакансию")
        return

    session["awaiting_repeat_choice"] = False
    await storage.save_session(session)
    vacancy = _session_vacancy(session, settings) if _has_selected_vacancy(session) else get_vacancy_snapshot(settings)
    if session.get("state") == "completed":
        text = get_msg("completed")
    elif session.get("state") == "blocked":
        text = get_msg("blocked", seconds=_remaining_block_seconds(session))
    elif session.get("resume_text"):
        text = (
            f"Сейчас активна вакансия <b>{vacancy['title']}</b>. "
            "Если хотите продолжить, просто ответьте следующим сообщением."
        )
    else:
        text = get_msg("greeting", title=vacancy["title"])
    _record_assistant_message(session, text, source="repeat_continue")
    await storage.save_session(session)
    await callback.message.edit_text(text, parse_mode="HTML")
    await callback.answer()


@router.message()
async def handle_candidate_message(message: Message):
    if message.chat.type != "private":
        return
    if message.text and message.text.startswith("/"):
        return

    user_id = message.from_user.id
    is_new_session = not storage.session_exists(user_id)
    session = await storage.load_session(user_id)
    session["username"] = _candidate_name(message)
    settings = load_settings()

    if is_new_session:
        session["awaiting_vacancy_choice"] = True
        incoming_text = _message_history_text(message)
        _record_candidate_message(session, incoming_text, source="candidate_initial")
        storage.add_session_event(
            session,
            "candidate",
            "session_started",
            source="first_message",
        )
        await storage.save_session(session)
        _log_candidate(
            logging.INFO,
            user_id,
            "session_started",
            source="first_message",
            username=session["username"],
        )
        greeting_text = get_msg("vacancy_choice")
        _record_assistant_message(session, greeting_text, source="greeting")
        await storage.save_session(session)
        await message.answer(
            greeting_text,
            parse_mode="HTML",
            reply_markup=_vacancy_choice_keyboard(settings),
        )
        if _looks_like_resume_payload(message):
            prompt_text = (
                "Сначала коротко познакомился. Теперь пришлите, пожалуйста, резюме еще раз одним сообщением или PDF-файлом."
            )
            _record_assistant_message(session, prompt_text, source="resume_prompt")
            await storage.save_session(session)
            await message.answer(
                prompt_text,
                reply_markup=_vacancy_choice_keyboard(settings),
            )
        return

    if session.get("awaiting_repeat_choice"):
        _record_candidate_message(
            session,
            _message_history_text(message),
            source="candidate_before_repeat_choice",
        )
        reminder_text = (
            "Выберите действие кнопками выше: обновить резюме, сменить вакансию или продолжить."
        )
        _record_assistant_message(session, reminder_text, source="repeat_prompt")
        await storage.save_session(session)
        await message.answer(reminder_text, reply_markup=_repeat_keyboard())
        return

    if session.get("awaiting_vacancy_choice") or not _has_selected_vacancy(session):
        _record_candidate_message(
            session,
            _message_history_text(message),
            source="candidate_before_vacancy_choice",
        )
        reminder_text = get_msg("vacancy_choose_first")
        _record_assistant_message(session, reminder_text, source="vacancy_prompt")
        await storage.save_session(session)
        await message.answer(
            reminder_text,
            reply_markup=_vacancy_choice_keyboard(settings),
        )
        return

    current_state = await _normalize_block_state(session)
    await storage.save_session(session)

    if current_state == "blocked":
        _record_candidate_message(
            session,
            _message_history_text(message),
            source="candidate_while_blocked",
        )
        _log_candidate(
            logging.INFO,
            user_id,
            "message_rejected_blocked",
            remaining_seconds=_remaining_block_seconds(session),
        )
        if session.get("permanent_block") and session.get("block_reason") == "vacancy_mismatch":
            blocked_text = (
                "Спасибо за отклик. Сейчас мы не продолжаем интервью, потому что профиль резюме не совпадает с вакансией."
            )
            _record_assistant_message(session, blocked_text, source="blocked")
            await storage.save_session(session)
            await message.answer(blocked_text)
        else:
            blocked_text = get_msg("blocked", seconds=_remaining_block_seconds(session))
            _record_assistant_message(session, blocked_text, source="blocked")
            await storage.save_session(session)
            await message.answer(blocked_text)
        return

    if current_state in {"completed", "waiting_decision"}:
        _record_candidate_message(
            session,
            _message_history_text(message),
            source="candidate_after_complete",
        )
        _log_candidate(logging.INFO, user_id, "message_ignored_completed")
        completed_text = get_msg("completed")
        _record_assistant_message(session, completed_text, source="completed")
        await storage.save_session(session)
        await message.answer(completed_text)
        return

    if current_state == "interviewing":
        await _handle_interview_message(message, session)
        return

    await _handle_resume_message(message, session)


async def _handle_resume_message(message: Message, session: dict):
    user_id = session["user_id"]
    resume_text = None
    resume_source = None
    incoming_text = _message_history_text(message)
    vacancy = _session_vacancy(session)

    if message.document and message.document.mime_type == "application/pdf":
        resume_source = "pdf"
        _record_candidate_message(session, incoming_text, source="resume_pdf")
        _log_candidate(
            logging.INFO,
            user_id,
            "resume_file_received",
            source=resume_source,
            file_name=message.document.file_name or "-",
        )
        resume_text = await _extract_pdf_text(message, user_id)
        if not resume_text:
            _log_candidate(logging.WARNING, user_id, "resume_pdf_extract_failed")
            reply_text = get_msg("resume_read_failed")
            _record_assistant_message(session, reply_text, source="resume_error")
            await storage.save_session(session)
            await message.answer(reply_text)
            return
    elif message.text:
        text = message.text.strip()
        if not _looks_like_resume_text(text):
            _record_candidate_message(session, incoming_text, source="candidate_pre_resume")
            if await ai_client.check_off_topic(text):
                _log_candidate(
                    logging.INFO,
                    user_id,
                    "resume_rejected_off_topic",
                    chars=len(text),
                )
                await _block_user(message, session)
                return
            _log_candidate(
                logging.INFO,
                user_id,
                "resume_prompt_repeated",
                chars=len(text),
            )
            reply_text = get_msg("send_resume")
            _record_assistant_message(session, reply_text, source="resume_prompt")
            await storage.save_session(session)
            await message.answer(reply_text)
            return
        _record_candidate_message(session, incoming_text, source="resume_text")
        if await ai_client.check_off_topic(text):
            _log_candidate(
                logging.INFO,
                user_id,
                "resume_rejected_off_topic",
                chars=len(text),
            )
            await _block_user(message, session)
            return
        if len(text) < 50:
            _log_candidate(
                logging.INFO,
                user_id,
                "resume_rejected_too_short",
                chars=len(text),
            )
            reply_text = get_msg("resume_too_short")
            _record_assistant_message(session, reply_text, source="resume_error")
            await storage.save_session(session)
            await message.answer(reply_text)
            return
        resume_source = "text"
        resume_text = text
    else:
        _record_candidate_message(session, incoming_text, source="candidate_pre_resume")
        _log_candidate(
            logging.INFO,
            user_id,
            "resume_rejected_unsupported_payload",
            has_document=bool(message.document),
            has_text=bool(message.text),
        )
        reply_text = get_msg("send_resume")
        _record_assistant_message(session, reply_text, source="resume_prompt")
        await storage.save_session(session)
        await message.answer(reply_text)
        return

    session["resume_text"] = resume_text
    session["resume_analysis"] = None
    session["resume_screening"] = None
    session["candidate_score"] = None
    session["interview_notes"] = []
    session["summary_saved"] = False
    session["summary_path"] = None
    session["state"] = "waiting_resume"
    session["permanent_block"] = False
    session["block_reason"] = None
    session["employer_decision"] = "pending"
    _init_interview_script(session)
    storage.add_session_event(
        session,
        "candidate",
        "resume_received",
        source=resume_source,
        chars=len(resume_text),
    )
    await storage.save_session(session)
    _log_candidate(
        logging.INFO,
        user_id,
        "resume_received",
        source=resume_source,
        chars=len(resume_text),
    )

    status_text = "Резюме получил. Быстро проверю его и подготовлю вопросы."
    _record_assistant_message(session, status_text, source="resume_status")
    await storage.save_session(session)
    status_message = await _answer_with_typing(message, status_text)
    _log_candidate(
        logging.INFO,
        user_id,
        "resume_analysis_started",
        source=resume_source,
    )

    try:
        async with _typing_indicator(message):
            screening = await ai_client.screen_resume(resume_text, vacancy=vacancy)
    except Exception as error:
        _log_candidate(
            logging.ERROR,
            user_id,
            "resume_screening_failed",
            error=f"{type(error).__name__}: {error}",
        )
        reply_text = "Ошибка анализа резюме. Проверьте OpenRouter и попробуйте позже."
        _record_assistant_message(session, reply_text, source="resume_error")
        await storage.save_session(session)
        await status_message.edit_text(reply_text)
        return

    analysis = screening.get("employer_summary", "").strip()
    if not analysis:
        try:
            async with _typing_indicator(message):
                analysis = await ai_client.analyze_resume(resume_text, vacancy=vacancy)
        except Exception as error:
            _log_candidate(
                logging.ERROR,
                user_id,
                "resume_analysis_failed",
                error=f"{type(error).__name__}: {error}",
            )
            reply_text = "Ошибка анализа резюме. Проверьте OpenRouter и попробуйте позже."
            _record_assistant_message(session, reply_text, source="resume_error")
            await storage.save_session(session)
            await status_message.edit_text(reply_text)
            return

    screening = _enforce_profession_gate(
        screening,
        vacancy=vacancy,
        resume_text=resume_text,
    )

    session["resume_analysis"] = analysis
    session["resume_screening"] = screening
    storage.add_session_event(
        session,
        "system",
        "resume_analysis_completed",
        analysis_chars=len(analysis),
        fit_score=screening.get("fit_score"),
        should_reject=screening.get("should_reject"),
    )
    _log_candidate(
        logging.INFO,
        user_id,
        "resume_analysis_completed",
        analysis_chars=len(analysis),
        fit_score=screening.get("fit_score"),
        should_reject=screening.get("should_reject"),
    )

    if screening.get("should_reject"):
        session["state"] = "blocked"
        session["permanent_block"] = True
        session["block_reason"] = "vacancy_mismatch"
        session["return_state"] = None
        session["block_until"] = None
        storage.add_session_event(
            session,
            "system",
            "resume_rejected",
            fit_score=screening.get("fit_score"),
            reason=analysis,
        )
        await storage.save_session(session)
        _log_candidate(
            logging.WARNING,
            user_id,
            "resume_rejected",
            fit_score=screening.get("fit_score"),
        )
        await _notify_employers_resume_analysis(message.bot, session, analysis)
        reply_text = (
            screening.get("candidate_message")
            or "Спасибо за отклик. Сейчас мы не продолжаем интервью, потому что профиль резюме не совпадает с вакансией."
        )
        _record_assistant_message(session, reply_text, source="resume_rejected")
        await storage.save_session(session)
        await status_message.edit_text(reply_text)
        return

    session["state"] = "interviewing"
    session["permanent_block"] = False
    session["block_reason"] = None
    _init_interview_script(session)
    _interview_topics(session)
    await storage.save_session(session)
    await _notify_employers_resume_analysis(message.bot, session, analysis)

    reply_text = (
        "Спасибо, резюме изучил. Теперь задам несколько коротких вопросов по опыту и условиям работы."
    )
    _record_assistant_message(session, reply_text, source="resume_accepted")
    await storage.save_session(session)
    await status_message.edit_text(reply_text)
    await _ask_next_question(message, session)


async def _handle_interview_message(message: Message, session: dict):
    user_id = session["user_id"]
    if not message.text:
        _record_candidate_message(
            session,
            _message_history_text(message),
            source="candidate_non_text",
        )
        _log_candidate(logging.INFO, user_id, "interview_rejected_non_text")
        reply_text = get_msg("resume_only_text")
        _record_assistant_message(session, reply_text, source="interview_error")
        await storage.save_session(session)
        await message.answer(reply_text)
        return

    text = message.text.strip()
    if not text:
        _record_candidate_message(session, _message_history_text(message), source="candidate_empty")
        _log_candidate(logging.INFO, user_id, "interview_rejected_empty_text")
        reply_text = get_msg("resume_only_text")
        _record_assistant_message(session, reply_text, source="interview_error")
        await storage.save_session(session)
        await message.answer(reply_text)
        return

    if not _is_contextual_reference_answer(text) and await ai_client.check_off_topic(text):
        _record_candidate_message(session, text, source="candidate_off_topic")
        _log_candidate(
            logging.INFO,
            user_id,
            "interview_rejected_off_topic",
            chars=len(text),
        )
        await _block_user(message, session)
        return

    storage.add_dialog_message(session, "user", text, source="candidate")
    storage.add_session_event(
        session,
        "candidate",
        "interview_answer_received",
        answer_index=_interview_answer_count(session),
        chars=len(text),
    )
    await storage.save_session(session)
    _log_candidate(
        logging.INFO,
        user_id,
        "interview_answer_received",
        answer_index=_interview_answer_count(session),
        chars=len(text),
    )

    topic = _current_interview_topic(session)
    if topic is None:
        await _finish_interview(message, session)
        return

    question_text = session.get("last_question_text") or topic["question"]
    try:
        async with _typing_indicator(message):
            assessment = await ai_client.assess_interview_answer(
                topic_name=topic["name"],
                topic_goal=topic["goal"],
                question_text=question_text,
                answer_text=text,
            )
    except Exception as error:
        _log_candidate(
            logging.WARNING,
            user_id,
            "interview_answer_assessment_failed",
            error=f"{type(error).__name__}: {error}",
        )
        assessment = {
            "relevant": True,
            "sufficient": not _is_brief_or_vague_answer(text),
            "follow_up_needed": _is_brief_or_vague_answer(text),
            "suggested_follow_up_question": "",
            "short_reason": "Локальная эвристика",
            "extracted_facts": [text],
            "missing_points": (
                ["Нужен более конкретный ответ"]
                if _is_brief_or_vague_answer(text) else []
            ),
        }

    session.setdefault("interview_notes", []).append(
        {
            "topic_id": topic["id"],
            "topic_key": topic.get("key") or topic["id"],
            "topic_name": topic["name"],
            "gap_label": topic.get("gap_label") or topic["name"],
            "question": question_text,
            "answer": text,
            "relevant": assessment.get("relevant", True),
            "sufficient": assessment.get("sufficient", False),
            "reason": assessment.get("short_reason", ""),
            "facts": assessment.get("extracted_facts", []),
            "missing_points": assessment.get("missing_points", []),
            "at": datetime.now().isoformat(),
        }
    )
    session["interview_notes"] = session["interview_notes"][-20:]
    storage.add_session_event(
        session,
        "system",
        "interview_answer_assessed",
        topic=topic["id"],
        sufficient=assessment.get("sufficient", False),
        relevant=assessment.get("relevant", True),
        reason=assessment.get("short_reason", ""),
    )
    await storage.save_session(session)
    _log_candidate(
        logging.INFO,
        user_id,
        "interview_answer_assessed",
        topic=topic["id"],
        sufficient=assessment.get("sufficient", False),
        relevant=assessment.get("relevant", True),
    )

    if assessment.get("follow_up_needed", not assessment.get("sufficient", False)) and not session.get("interview_followup_used"):
        await _send_script_question(
            message,
            session,
            follow_up=True,
            assessment=assessment,
        )
        return

    session["interview_topic_index"] = int(session.get("interview_topic_index", 0)) + 1
    session["interview_followup_used"] = False
    if _current_interview_topic(session) is None:
        await _finish_interview(message, session)
        return

    await _send_script_question(
        message,
        session,
        follow_up=False,
        assessment=assessment,
    )


async def _finish_interview(message: Message, session: dict):
    session["state"] = "waiting_decision"
    storage.add_session_event(
        session,
        "system",
        "interview_completed",
        answers=_interview_answer_count(session),
    )
    await storage.save_session(session)
    _log_candidate(
        logging.INFO,
        session["user_id"],
        "interview_completed",
        answers=_interview_answer_count(session),
    )

    completion_text = get_msg("interview_complete")
    _record_assistant_message(session, completion_text, source="interview_complete")
    await storage.save_session(session)
    await _answer_with_typing(message, completion_text)

    settings = load_settings()
    vacancy = _session_vacancy(session, settings)
    current_dialog = _history_for_ai(session)
    interview_notes = session.get("interview_notes") or []
    candidate_score = None
    threshold = int(vacancy.get("score_threshold", 28))

    try:
        candidate_score = await ai_client.score_candidate(
            vacancy=vacancy,
            resume_text=session.get("resume_text") or "",
            screening=session.get("resume_screening") or {},
            interview_notes=interview_notes,
            dialog=current_dialog,
        )
    except Exception as error:
        _log_candidate(
            logging.WARNING,
            session["user_id"],
            "candidate_scoring_failed",
            error=f"{type(error).__name__}: {error}",
        )
        candidate_score = _build_local_candidate_score(session, threshold=threshold)

    session["candidate_score"] = candidate_score
    storage.add_session_event(
        session,
        "system",
        "candidate_scored",
        overall_score=candidate_score.get("overall_score"),
        threshold=candidate_score.get("threshold"),
        passed_threshold=candidate_score.get("passed_threshold"),
    )
    await storage.save_session(session)

    _log_candidate(
        logging.INFO,
        session["user_id"],
        "summary_generation_started",
        vacancy=vacancy["title"],
    )
    ai_summary = None
    try:
        ai_summary = await ai_client.generate_summary(
            resume=session["resume_text"] or "",
            analysis=session["resume_analysis"] or "",
            dialog=current_dialog,
            title=vacancy["title"],
            screening=session.get("resume_screening") or {},
            interview_notes=interview_notes,
        )
    except Exception as error:
        _log_candidate(
            logging.WARNING,
            session["user_id"],
            "summary_ai_generation_failed",
            error=f"{type(error).__name__}: {error}",
        )

    try:
        summary_document = _compose_final_summary(settings, session, ai_summary)
        path = await storage.save_summary(
            session["user_id"],
            session["username"] or str(session["user_id"]),
            summary_document,
        )
        summary_path = str(path)
        session["summary_saved"] = True
        session["summary_path"] = summary_path
        storage.add_session_event(session, "system", "summary_saved", path=summary_path)
        await storage.save_session(session)
        _log_candidate(
            logging.INFO,
            session["user_id"],
            "summary_saved",
            path=summary_path,
        )
        await _notify_employers_summary_ready(message.bot, session, summary_path)
    except Exception as error:
        _log_candidate(
            logging.ERROR,
            session["user_id"],
            "summary_generation_failed",
            error=f"{type(error).__name__}: {error}",
        )


def _compose_final_summary(settings: dict, session: dict, ai_summary: str | None) -> str:
    vacancy = _session_vacancy(session, settings)
    screening = session.get("resume_screening") or {}
    notes = session.get("interview_notes") or []
    score = session.get("candidate_score") or {}
    missing_before = screening.get("missing_information") or []
    clarified_points, remaining_points = _interview_summary_sections(
        notes,
        missing_before,
    )
    transcript_lines = []
    for item in _current_round_history(session):
        role = "Кандидат" if item.get("role") == "user" else "HR"
        transcript_lines.append(f"{role}: {item.get('content', '')}")

    interview_details = []
    for index, item in enumerate(notes, start=1):
        facts = item.get("facts") or []
        missing = item.get("missing_points") or []
        interview_details.append(
            "\n".join(
                [
                    f"{index}. Тема: {item.get('gap_label') or item.get('topic_name', '-')}",
                    f"Вопрос: {item.get('question', '-')}",
                    f"Ответ: {item.get('answer', '-')}",
                    f"Оценка: {'достаточно' if item.get('sufficient') else 'нужно уточнение'}; {'релевантно' if item.get('relevant', True) else 'сомнительно'}",
                    f"Факты: {'; '.join(facts) if facts else '-'}",
                    f"Пробелы: {'; '.join(missing) if missing else '-'}",
                ]
            )
        )

    parts = [
        f"Вакансия: {vacancy['title']}",
        f"Кандидат: {session.get('username') or session['user_id']}",
        f"ID: {session['user_id']}",
        "",
        "=== РЕЗЮМЕ КАНДИДАТА ===",
        session.get("resume_text") or "-",
        "",
        "=== СКРИНИНГ РЕЗЮМЕ ===",
        f"Fit score: {screening.get('fit_score', '-')}/10",
        f"Профиль совпадает с вакансией: {'да' if screening.get('profession_match', True) else 'нет'}",
        f"Совпадения: {'; '.join(screening.get('key_matches', [])) if screening.get('key_matches') else '-'}",
        f"Пробелы: {'; '.join(screening.get('key_gaps', [])) if screening.get('key_gaps') else '-'}",
        f"Чего не хватало в резюме: {'; '.join(missing_before) if missing_before else '-'}",
        "",
        "=== АНАЛИЗ РЕЗЮМЕ ===",
        session.get("resume_analysis") or "-",
        "",
        "=== ЧТО НУЖНО БЫЛО УТОЧНИТЬ НА СОБЕСЕДОВАНИИ ===",
        "\n".join(f"- {item}" for item in missing_before) if missing_before else "Критичных пробелов в резюме не выявлено.",
        "",
        "=== ЧТО УДАЛОСЬ УТОЧНИТЬ ===",
        "\n".join(f"- {item}" for item in clarified_points) if clarified_points else "Подтвержденных уточнений пока не зафиксировано.",
        "",
        "=== ЧТО ОСТАЛОСЬ НЕВЫЯСНЕННЫМ ===",
        "\n".join(f"- {item}" for item in remaining_points) if remaining_points else "Критичных открытых вопросов не осталось.",
        "",
        "=== SCORING КАНДИДАТА ===",
        f"Опыт: {score.get('experience_score', '-')}/10",
        f"Навыки: {score.get('skills_score', '-')}/10",
        f"Мотивация: {score.get('motivation_score', '-')}/10",
        f"Культурный fit: {score.get('culture_fit_score', '-')}/10",
        f"Итоговый балл: {score.get('overall_score', '-')}/40",
        f"Порог вакансии: {score.get('threshold', vacancy.get('score_threshold', '-'))}/40",
        f"Сравнение с порогом: {'порог пройден' if score.get('passed_threshold') else 'ниже порога'}",
        f"Сильные стороны: {'; '.join(score.get('strengths', [])) if score.get('strengths') else '-'}",
        f"Риски: {'; '.join(score.get('risks', [])) if score.get('risks') else '-'}",
        f"Комментарий по score: {score.get('employer_summary') or '-'}",
        "",
        "=== КЛЮЧЕВЫЕ ОТВЕТЫ СОБЕСЕДОВАНИЯ ===",
        "\n\n".join(interview_details) if interview_details else "Интервью-заметки не собраны.",
        "",
        "=== ПОЛНЫЙ ДИАЛОГ ===",
        "\n".join(transcript_lines) if transcript_lines else "Диалог отсутствует.",
        "",
        "=== ИТОГОВАЯ РЕКОМЕНДАЦИЯ ===",
        _recommendation_from_screening(screening, notes, score),
    ]

    if ai_summary:
        parts.extend(
            [
                "",
                "=== AI-СВОДКА ===",
                ai_summary.strip(),
            ]
        )

    return "\n".join(parts).strip() + "\n"


def _interview_summary_sections(
    notes: list[dict],
    missing_before: list[str],
) -> tuple[list[str], list[str]]:
    clarified: list[str] = []
    remaining: list[str] = []
    covered_keys: set[str] = set()

    for item in notes:
        label = item.get("gap_label") or item.get("topic_name") or "Уточнение"
        key = _normalized_text(label)
        facts = [fact for fact in (item.get("facts") or []) if str(fact).strip()]
        missing = [point for point in (item.get("missing_points") or []) if str(point).strip()]

        if item.get("sufficient"):
            covered_keys.add(key)
            if facts:
                clarified.append(f"{label}: {'; '.join(facts)}")
            else:
                clarified.append(f"{label}: кандидат дал достаточное пояснение.")

        if missing:
            remaining.append(f"{label}: {'; '.join(missing)}")

    for raw_item in missing_before:
        item = " ".join(str(raw_item or "").split()).strip()
        key = _normalized_text(item)
        if item and key and key not in covered_keys:
            remaining.append(f"{item}: на интервью не получили полного подтверждения.")

    return _deduplicate_summary_items(clarified), _deduplicate_summary_items(remaining)


def _deduplicate_summary_items(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = " ".join(str(item or "").split()).strip()
        key = _normalized_text(text)
        if not text or not key or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _build_local_candidate_score(session: dict, *, threshold: int) -> dict:
    screening = session.get("resume_screening") or {}
    notes = session.get("interview_notes") or []
    fit_score = max(1, min(10, int(screening.get("fit_score") or 5)))
    sufficient_answers = sum(1 for item in notes if item.get("sufficient"))
    note_count = max(1, len(notes))
    experience = max(1, min(10, fit_score + (1 if sufficient_answers >= 2 else 0)))
    skills = max(1, min(10, fit_score + (1 if sufficient_answers >= note_count // 2 else 0)))
    motivation = max(1, min(10, 5 + (1 if sufficient_answers >= 2 else 0)))
    culture_fit = max(1, min(10, 5 + (1 if sufficient_answers >= 3 else 0)))
    overall = experience + skills + motivation + culture_fit
    return {
        "experience_score": experience,
        "skills_score": skills,
        "motivation_score": motivation,
        "culture_fit_score": culture_fit,
        "overall_score": overall,
        "average_score": round(overall / 4, 1),
        "threshold": threshold,
        "passed_threshold": overall >= threshold,
        "strengths": list(screening.get("key_matches") or [])[:5],
        "risks": list(screening.get("key_gaps") or [])[:5],
        "employer_summary": "Локальная оценка построена по screening и ответам интервью.",
    }


def _recommendation_from_screening(screening: dict, notes: list[dict], score: dict | None = None) -> str:
    try:
        fit_score = int(screening.get("fit_score") or 0)
    except (TypeError, ValueError):
        fit_score = 0
    sufficient_answers = sum(1 for item in notes if item.get("sufficient"))
    overall_score = None
    passed_threshold = None
    if isinstance(score, dict):
        try:
            overall_score = int(score.get("overall_score"))
        except (TypeError, ValueError):
            overall_score = None
        passed_threshold = score.get("passed_threshold")
    if screening.get("should_reject"):
        return "ОТКАЗАТЬ: профиль резюме не соответствует вакансии."
    if passed_threshold is True and overall_score is not None and overall_score >= 34:
        return "НАНЯТЬ / ПРИОРИТЕТНО РАССМОТРЕТЬ: кандидат прошел порог по score и дал сильные ответы на интервью."
    if passed_threshold is True:
        return "РАССМОТРЕТЬ / ПЕРЕВЕСТИ ДАЛЬШЕ: кандидат прошел порог вакансии, но перед оффером стоит подтвердить отдельные детали."
    if overall_score is not None and overall_score < 24:
        return "ОТКАЗАТЬ: итоговый score заметно ниже порога вакансии и ответы не подтвердили достаточную глубину."
    if fit_score >= 8 and sufficient_answers >= 3:
        return "НАНЯТЬ / ПРИОРИТЕТНО РАССМОТРЕТЬ: профиль сильный, ответы по существу."
    if fit_score >= 5:
        return "РАССМОТРЕТЬ: профиль в целом подходит, но нужны дополнительные проверки."
    return "ОТКАЗАТЬ: мало совпадений по вакансии и/или слишком слабые ответы."


async def _normalize_block_state(session: dict) -> str:
    if session.get("state") != "blocked":
        return session.get("state", "waiting_resume")

    if session.get("permanent_block"):
        return "blocked"

    block_until = _parse_block_time(session.get("block_until"))
    if not block_until or datetime.now() >= block_until:
        session["state"] = session.get("return_state") or _default_active_state(session)
        session["return_state"] = None
        session["block_until"] = None
        storage.add_session_event(
            session,
            "system",
            "block_expired",
            restored_state=session["state"],
        )
        _log_candidate(
            logging.INFO,
            session["user_id"],
            "block_expired",
            restored_state=session["state"],
        )
    return session.get("state", "waiting_resume")


async def _block_user(message: Message, session: dict):
    duration = max(1, int(load_settings().get("block_duration_seconds", 10)))
    previous_state = session.get("state") or _default_active_state(session)

    session["off_topic_count"] = session.get("off_topic_count", 0) + 1
    session["return_state"] = (
        previous_state if previous_state != "blocked" else _default_active_state(session)
    )
    session["state"] = "blocked"
    session["block_until"] = (
        datetime.now() + timedelta(seconds=duration)
    ).isoformat()
    storage.add_session_event(
        session,
        "system",
        "candidate_blocked",
        seconds=duration,
        previous_state=previous_state,
        off_topic_count=session["off_topic_count"],
    )
    await storage.save_session(session)
    _log_candidate(
        logging.WARNING,
        session["user_id"],
        "candidate_blocked",
        seconds=duration,
        previous_state=previous_state,
        off_topic_count=session["off_topic_count"],
    )

    reply_text = get_msg("off_topic", seconds=duration)
    _record_assistant_message(session, reply_text, source="off_topic")
    await storage.save_session(session)
    await message.answer(reply_text, parse_mode="HTML")


async def _ask_next_question(message: Message, session: dict):
    session["interview_topic_index"] = 0
    session["interview_followup_used"] = False
    await _send_script_question(message, session, follow_up=False)


async def _generate_interview_reply(session: dict) -> str:
    raw_reply = await ai_client.interview_reply(
        _history_for_ai(session),
        _build_interview_prompt(session),
    )
    normalized = _normalize_interview_reply(raw_reply)
    if not _interview_reply_needs_repair(normalized):
        return normalized

    _log_candidate(
        logging.WARNING,
        session["user_id"],
        "interview_reply_flagged_for_repair",
        preview=_trim_text(raw_reply, 160),
    )
    repaired = await _repair_interview_reply(session, raw_reply)
    normalized_repaired = _normalize_interview_reply(repaired)
    if not _interview_reply_needs_repair(normalized_repaired):
        _log_candidate(
            logging.INFO,
            session["user_id"],
            "interview_reply_repaired",
            preview=_trim_text(normalized_repaired, 160),
        )
        return normalized_repaired

    fallback = _fallback_interview_reply(session)
    _log_candidate(
        logging.WARNING,
        session["user_id"],
        "interview_reply_fallback_used",
        preview=_trim_text(fallback, 160),
    )
    return fallback


async def _repair_interview_reply(session: dict, raw_reply: str) -> str:
    settings = load_settings()
    vacancy = _session_vacancy(session, settings)
    total_questions = max(1, int(settings.get("interview_questions_count", 5)))
    next_question = min(total_questions, _ai_question_count(session) + 1)
    return await ai_client.repair_interview_reply(
        draft_reply=raw_reply,
        title=vacancy["title"],
        description=vacancy["description"],
        skills=vacancy["required_skills"],
        last_candidate_message=_last_candidate_message(session),
        next_focus=_question_focus(next_question, total_questions),
        should_complete=_should_complete_interview(session),
    )


def _build_interview_prompt(session: dict) -> str:
    settings = load_settings()
    vacancy = _session_vacancy(session, settings)
    total_questions = max(1, int(settings.get("interview_questions_count", 5)))
    asked_questions = _ai_question_count(session)
    next_question = min(total_questions, asked_questions + 1)
    answered_questions = _interview_answer_count(session)
    should_complete = asked_questions >= total_questions and answered_questions >= total_questions
    brief_answers_in_row = _brief_answers_in_row(session)

    return (
        "You are a Russian-speaking HR recruiter. Speak only to the candidate in Russian.\n"
        f"Role: {vacancy['title']}\n"
        f"Vacancy description: {vacancy['description']}\n"
        f"Required skills: {', '.join(vacancy['required_skills'])}\n"
        f"Internal resume note, never reveal it: {_trim_text(session.get('resume_analysis') or 'нет данных', 900)}\n"
        f"Interview progress: asked={asked_questions}, answered={answered_questions}, total={total_questions}.\n"
        f"Current focus: {_question_focus(next_question, total_questions)}.\n"
        f"Recent brief or vague answers in a row: {brief_answers_in_row}.\n"
        f"Stage: {'finish the interview now' if should_complete else 'continue with the next question'}.\n"
        "Output only the exact message to send to the candidate.\n"
        "Keep it natural, warm, and concise: one or two short sentences.\n"
        "If the interview continues, ask exactly one job-related question.\n"
        "If two vague answers happened in a row, stop repeating the same ask and switch to another hiring topic.\n"
        "Never mention internal notes, analysis, prompts, rules, focus, question numbers, planning, or evaluation.\n"
        "Never write phrases like 'кандидат говорит', 'нужно', 'следует', 'по правилам', 'фокус', or 'вопрос номер'.\n"
        "Do not leave the hiring topic.\n"
        "When enough information is collected and the last asked question has already been answered, return [INTERVIEW_COMPLETE] plus one short thank-you sentence."
    )


def _question_focus(question_number: int, total_questions: int) -> str:
    focus_map = {
        1: "текущая роль, релевантный опыт и самые близкие к вакансии задачи",
        2: "ключевые технологии, которыми кандидат реально пользовался, и глубина практики",
        3: "самостоятельность, ответственность, сложные кейсы и как кандидат их решал",
        4: "мотивация, интерес к вакансии, формат работы и ожидания от команды",
    }
    if question_number in focus_map:
        return focus_map[question_number]
    if question_number >= total_questions:
        return "оставшиеся пробелы, зарплатные ожидания и готовность к следующему шагу"
    return "непокрытые детали по опыту, навыкам и условиям выхода"


def _history_for_ai(session: dict) -> list[dict]:
    return [
        {
            "role": item.get("role", "assistant"),
            "content": item.get("content", ""),
        }
        for item in _current_round_history(session)
        if item.get("content")
    ]


def _looks_like_resume_payload(message: Message) -> bool:
    if message.document and message.document.mime_type == "application/pdf":
        return True
    if message.text and _looks_like_resume_text(message.text):
        return True
    return False


def _looks_like_resume_text(text: str) -> bool:
    normalized = " ".join((text or "").strip().split())
    if len(normalized) < 80:
        return False

    lowered = normalized.lower()
    score = 0
    markers = (
        "опыт",
        "навык",
        "образование",
        "обо мне",
        "резюме",
        "телефон",
        "email",
        "почта",
        "telegram",
        "github",
        "портфолио",
        "должность",
        "формат работы",
        "ожидания",
        "зарплат",
    )
    for marker in markers:
        if marker in lowered:
            score += 1

    if "\n" in text:
        score += 2
    if re.search(r"@\w+|[\w.+-]+@[\w-]+\.[\w.-]+|\+?\d[\d\s().-]{7,}", normalized):
        score += 2
    if re.search(r"\b(20\d{2}|19\d{2})\b", normalized):
        score += 1

    return score >= 2 or len(normalized) >= 180


def _candidate_name(message: Message) -> str:
    return message.from_user.username or message.from_user.full_name


def _parse_block_time(raw_value: str | None) -> datetime | None:
    if not raw_value:
        return None
    try:
        return datetime.fromisoformat(raw_value)
    except ValueError:
        return None


def _remaining_block_seconds(session: dict) -> int:
    block_until = _parse_block_time(session.get("block_until"))
    if not block_until:
        return 0
    return max(0, int((block_until - datetime.now()).total_seconds()))


def _default_active_state(session: dict) -> str:
    if session.get("candidate_score") or session.get("summary_saved"):
        if session.get("employer_decision") in {"approved", "rejected"}:
            return "completed"
        return "waiting_decision"
    if session.get("resume_text"):
        return "interviewing" if session.get("resume_analysis") else "waiting_resume"
    return "waiting_resume"


def _brief_answers_in_row(session: dict) -> int:
    count = 0
    for item in reversed(_current_round_history(session)):
        if item.get("role") != "user" or item.get("source") != "candidate":
            continue
        if _is_brief_or_vague_answer(item.get("content", "")):
            count += 1
            continue
        break
    return count


def _is_brief_or_vague_answer(text: str) -> bool:
    normalized = " ".join(text.strip().lower().split())
    if not normalized:
        return True
    if len(normalized) < 36:
        return True
    vague_patterns = (
        "ну я",
        "всяких",
        "разное",
        "программы делал",
        "участвовал",
        "делал программы",
        "делал сайты",
        "работал там",
        "разрабатывал github",
    )
    return any(pattern in normalized for pattern in vague_patterns)


def _is_contextual_reference_answer(text: str) -> bool:
    normalized = " ".join(text.strip().lower().split())
    if not normalized:
        return False
    reference_patterns = (
        "я уже сказала",
        "я уже писал",
        "я уже указала",
        "я уже указывал",
        "выше написала",
        "выше написал",
        "смотрите выше",
        "смотри выше",
        "как выше",
        "я писала выше",
        "я писал выше",
        "это было выше",
        "это уже было",
        "указано выше",
        "в резюме есть",
        "это в резюме",
    )
    return any(pattern in normalized for pattern in reference_patterns)


def _last_candidate_message(session: dict) -> str:
    for item in reversed(_current_round_history(session)):
        if item.get("role") == "user" and item.get("source") == "candidate":
            return item.get("content", "")
    return ""


def _should_complete_interview(session: dict) -> bool:
    total_questions = max(1, int(load_settings().get("interview_questions_count", 5)))
    return (
        _ai_question_count(session) >= total_questions
        and _interview_answer_count(session) >= total_questions
    )


def _normalize_interview_reply(text: str) -> str:
    if not text:
        return ""

    cleaned = text.replace("\u0000", "").replace("\r", "\n").strip()
    completion = "[INTERVIEW_COMPLETE]" in cleaned
    cleaned = cleaned.replace("[INTERVIEW_COMPLETE]", "").strip()

    marker_position = _meta_marker_position(cleaned)
    if marker_position is not None and marker_position > 0:
        cleaned = cleaned[:marker_position].strip()

    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", cleaned)
        if paragraph.strip()
    ]
    if paragraphs:
        cleaned = paragraphs[0]

    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if completion:
        return f"[INTERVIEW_COMPLETE] {cleaned}".strip()
    return cleaned


def _interview_reply_needs_repair(text: str) -> bool:
    if not text:
        return True

    completion = "[INTERVIEW_COMPLETE]" in text
    cleaned = text.replace("[INTERVIEW_COMPLETE]", "").strip()
    lowered = cleaned.lower()
    markers = (
        "кандидат говорит",
        "нужно ",
        "следует ",
        "по правилам",
        "фокус",
        "вопрос номер",
        "итак, вопрос",
        "интервью должно",
        "черновик",
        "внутрен",
        "правила",
        "draft",
        "stage:",
    )
    if any(marker in lowered for marker in markers):
        return True
    if len(cleaned) > 420:
        return True
    if not completion and cleaned.count("?") != 1:
        return True
    if completion and not cleaned:
        return True
    return False


def _meta_marker_position(text: str) -> int | None:
    lowered = text.lower()
    markers = [
        "\nфокус",
        "\nправила",
        "\nследует",
        "\nитак, вопрос",
        "\nоднако",
        "кандидат говорит",
        "вопрос номер",
        "по правилам",
        "stage:",
    ]
    positions = [lowered.find(marker) for marker in markers if lowered.find(marker) >= 0]
    return min(positions) if positions else None


def _fallback_interview_reply(session: dict) -> str:
    total_questions = max(1, int(load_settings().get("interview_questions_count", 5)))
    asked_questions = _ai_question_count(session)
    answered_questions = _interview_answer_count(session)
    if asked_questions >= total_questions and answered_questions >= total_questions:
        return "[INTERVIEW_COMPLETE] Спасибо за ответы. Мы изучим информацию и вернёмся к вам с обратной связью."

    next_question = min(total_questions, asked_questions + 1)
    templates = {
        1: "Спасибо. Расскажите, пожалуйста, о последнем проекте или задаче, где вы лично занимались наиболее релевантной для этой вакансии работой?",
        2: "Какие технологии и инструменты из вашего опыта вы реально использовали сами чаще всего, и что именно делали руками?",
        3: "Можете привести один конкретный пример сложной задачи, которую вы решали самостоятельно, и чем всё закончилось?",
        4: "Почему вам интересна эта вакансия и какой формат работы для вас сейчас предпочтителен?",
        5: "Какие у вас ожидания по зарплате и когда вы сможете выйти на работу, если мы договоримся?",
    }
    if _brief_answers_in_row(session) >= 2 and next_question < total_questions:
        next_question = min(total_questions, next_question + 1)
    return templates.get(
        next_question,
        "Подскажите, пожалуйста, какие у вас ожидания по условиям работы и когда вы готовы выйти?",
    )


async def _extract_pdf_text(message: Message, user_id: int) -> str | None:
    try:
        file = await message.bot.get_file(message.document.file_id)
        file_bytes = await message.bot.download_file(file.file_path)
        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes.read()))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        result = text.strip() or None
        _log_candidate(
            logging.INFO,
            user_id,
            "resume_pdf_parsed",
            pages=len(reader.pages),
            chars=len(result or ""),
        )
        return result
    except Exception as error:
        _log_candidate(
            logging.ERROR,
            user_id,
            "resume_pdf_parse_failed",
            error=f"{type(error).__name__}: {error}",
        )
        return None


async def _notify_employers_resume_analysis(bot: Bot, session: dict, analysis: str):
    settings = load_settings()
    employer_ids = settings.get("employer_ids", [])
    if not employer_ids:
        return
    vacancy = _session_vacancy(session, settings)
    screening = session.get("resume_screening") or {}

    text = (
        f"Новый кандидат: {session.get('username') or session['user_id']}\n"
        f"ID: {session['user_id']}\n"
        f"Вакансия: {vacancy['title']}\n"
        f"Fit score: {screening.get('fit_score', '-')}/10\n"
        f"Статус screening: {'reject' if screening.get('should_reject') else 'go_next'}\n\n"
        f"AI-анализ резюме:\n{_trim_text(analysis, 3200)}"
    )
    for employer_id in employer_ids:
        try:
            await bot.send_message(
                employer_id,
                text,
                reply_markup=_decision_keyboard(session["user_id"]),
            )
        except Exception as error:
            logger.warning(
                "candidate employer_analysis_notify_failed employer_id=%s user_id=%s error=%s",
                employer_id,
                session["user_id"],
                error,
            )


async def _notify_employers_summary_ready(bot: Bot, session: dict, path):
    settings = load_settings()
    employer_ids = settings.get("employer_ids", [])
    if not employer_ids:
        return
    vacancy = _session_vacancy(session, settings)
    score = session.get("candidate_score") or {}
    decision = session.get("employer_decision", "pending")

    text = (
        f"Собеседование завершено: {session.get('username') or session['user_id']}\n"
        f"ID: {session['user_id']}\n"
        f"Вакансия: {vacancy['title']}\n"
        f"Score: {score.get('overall_score', '-')}/40\n"
        f"Порог: {score.get('threshold', vacancy.get('score_threshold', '-'))}/40\n"
        f"Статус по score: {'порог пройден' if score.get('passed_threshold') else 'ниже порога'}\n"
        f"Решение работодателя: {decision}\n"
        f"Summary: {path}"
    )
    for employer_id in employer_ids:
        try:
            await bot.send_message(
                employer_id,
                text,
                reply_markup=_decision_keyboard(session["user_id"], include_summary=True),
            )
        except Exception as error:
            logger.warning(
                "candidate employer_summary_notify_failed employer_id=%s user_id=%s error=%s",
                employer_id,
                session["user_id"],
                error,
            )


def _trim_text(value: str, limit: int) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."
