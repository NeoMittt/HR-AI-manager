from __future__ import annotations

import copy
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BASE_DIR / "settings.json"

VACANCY_PRESETS = {
    "humanitarian": {
        "label": "Гуманитарное направление",
        "title": "HR Manager",
        "description": (
            "Ищем HR-менеджера для подбора персонала, коммуникации с кандидатами "
            "и сопровождения внутренних HR-процессов."
        ),
        "required_skills": [
            "Подбор персонала",
            "Интервьюирование",
            "Деловая коммуникация",
            "HR-документы",
        ],
        "score_threshold": 26,
    },
    "technical": {
        "label": "Техническое направление",
        "title": "Python Developer",
        "description": "Ищем Python-разработчика в команду. Опыт от 1 года.",
        "required_skills": ["Python", "SQL", "Git", "REST API"],
        "score_threshold": 28,
    },
    "economic": {
        "label": "Экономическое направление",
        "title": "Financial Analyst",
        "description": (
            "Ищем финансового аналитика для подготовки отчетности, план-факт анализа "
            "и расчета экономических показателей."
        ),
        "required_skills": [
            "Финансовый анализ",
            "Excel",
            "Бюджетирование",
            "Отчетность",
        ],
        "score_threshold": 27,
    },
    "industrial": {
        "label": "Промышленное направление",
        "title": "Production Engineer",
        "description": (
            "Ищем инженера производства для контроля процессов, качества, "
            "технической документации и соблюдения норм безопасности."
        ),
        "required_skills": [
            "Производственные процессы",
            "Техническая документация",
            "Контроль качества",
            "Промышленная безопасность",
        ],
        "score_threshold": 27,
    },
}

DEFAULT_SETTINGS = {
    "telegram_token": "",
    "openrouter_api_key": "",
    "openrouter_model": "nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter_free_models": [
        "nvidia/nemotron-3-super-120b-a12b:free",
        "openrouter/free",
        "google/gemma-3-12b-it:free",
        "google/gemma-3-4b-it:free",
    ],
    "employer_ids": [],
    "sessions_dir": "sessions",
    "summaries_dir": "sessions",
    "admin_host": "127.0.0.1",
    "admin_port": 8080,
    "block_duration_seconds": 10,
    "interview_questions_count": 5,
    "active_vacancy_preset": "technical",
    "open_vacancy_keys": ["humanitarian", "technical", "economic", "industrial"],
    "vacancy_presets": copy.deepcopy(VACANCY_PRESETS),
    "vacancy": {
        "title": VACANCY_PRESETS["technical"]["title"],
        "description": VACANCY_PRESETS["technical"]["description"],
        "required_skills": copy.deepcopy(
            VACANCY_PRESETS["technical"]["required_skills"]
        ),
        "score_threshold": VACANCY_PRESETS["technical"]["score_threshold"],
    },
    "messages": {
        "greeting": (
            "Привет! Я HR-бот компании.\n\n"
            "Сейчас мы общаемся по вакансии <b>{title}</b>.\n\n"
            "Следующим сообщением пришлите, пожалуйста, резюме PDF-файлом или текстом прямо в чат."
        ),
        "vacancy_choice": (
            "Привет! Я HR-бот компании.\n\n"
            "У нас открыто несколько вакансий. Выберите, пожалуйста, подходящее направление ниже, "
            "и после этого я попрошу резюме."
        ),
        "vacancy_choose_first": (
            "Сначала выберите, пожалуйста, вакансию кнопками ниже, а потом я попрошу резюме."
        ),
        "vacancy_selected": (
            "Отлично, выбрали вакансию <b>{title}</b>.\n\n"
            "Теперь пришлите, пожалуйста, резюме PDF-файлом или текстом прямо в чат."
        ),
        "repeat_options": (
            "Мы уже общались раньше по вакансии <b>{title}</b>.\n\n"
            "Можно обновить резюме для новой попытки, выбрать другую вакансию или просто посмотреть текущий статус."
        ),
        "repeat_resume_requested": (
            "Хорошо, начинаем новую попытку по вакансии <b>{title}</b>. "
            "Пришлите, пожалуйста, обновленное резюме."
        ),
        "repeat_choose_vacancy": (
            "Хорошо, выберите новую вакансию ниже. После выбора я попрошу резюме."
        ),
        "off_topic": (
            "Это сообщение не относится к теме трудоустройства.\n\n"
            "Сеанс приостановлен на <b>{seconds} сек.</b> После паузы можно продолжить."
        ),
        "resume_received": "Резюме получено. Анализирую...",
        "resume_analyzed": (
            "Резюме проверено.\n\n{analysis}\n\n"
            "Теперь задам несколько вопросов для знакомства."
        ),
        "interview_complete": (
            "Спасибо! Собеседование завершено. "
            "Мы сохранили итоговую сводку для работодателя."
        ),
        "blocked": (
            "Сеанс временно приостановлен. "
            "Подождите ещё {seconds} сек. и попробуйте снова."
        ),
        "session_restarted": "Пауза завершилась. Продолжаем диалог.",
        "completed": (
            "Ваше собеседование уже завершено. "
            "Если хотите обновить резюме или выбрать другую вакансию, нажмите /start."
        ),
        "resume_too_short": (
            "Пожалуйста, пришлите полное резюме текстом или PDF-файлом."
        ),
        "resume_only_text": "Во время интервью, пожалуйста, отвечайте текстом.",
        "send_resume": "Пришлите резюме PDF-файлом или текстом.",
        "resume_read_failed": (
            "Не удалось прочитать PDF. Пришлите, пожалуйста, резюме текстом."
        ),
        "interview_resume_first": (
            "Сначала нужно прислать резюме, после этого начнём диалог."
        ),
    },
    "prompts": {
        "off_topic_check": (
            "Пользователь пишет боту для трудоустройства. Сообщение: \"{message}\"\n\n"
            "Это сообщение по теме трудоустройства "
            "(резюме, опыт работы, навыки, вакансия, зарплата, условия, вопросы о работе)? "
            "Ответь только одним словом: YES или NO."
        ),
        "resume_analysis": (
            "Ты HR-менеджер. Вакансия: {title}\n"
            "Описание: {description}\n"
            "Требуемые навыки: {skills}\n\n"
            "Резюме кандидата:\n{resume}\n\n"
            "Дай краткую оценку: соответствие вакансии (1-10), "
            "сильные стороны, слабые стороны, пробелы. Будь конкретен, 4-6 предложений."
        ),
        "interview_system": (
            "Ты HR-менеджер компании. Проводишь собеседование на должность {title}.\n"
            "Описание вакансии: {description}\n\n"
            "Задай кандидату ровно {count} вопросов по одному за раз. "
            "Нужно выяснить опыт, стек, мотивацию, формат работы, зарплатные ожидания "
            "и другие важные детали. После ответа кандидата задавай только следующий вопрос. "
            "После финального вопроса и ответа верни [INTERVIEW_COMPLETE] и короткое спасибо."
        ),
        "summary": (
            "Составь структурированную сводку для работодателя по итогам собеседования.\n\n"
            "Вакансия: {title}\n\n"
            "Резюме кандидата:\n{resume}\n\n"
            "Оценка резюме:\n{analysis}\n\n"
            "Диалог собеседования:\n{dialog}\n\n"
            "Сводка должна включать:\n"
            "1. Имя и контакты, если они есть в резюме\n"
            "2. Краткий профиль кандидата\n"
            "3. Оценку резюме и риски\n"
            "4. Ключевые ответы на вопросы\n"
            "5. Итоговую рекомендацию: НАНЯТЬ / РАССМОТРЕТЬ / ОТКАЗАТЬ\n"
            "6. Комментарий HR"
        ),
    },
}


def _deep_merge(defaults: dict, overrides: dict) -> dict:
    merged = copy.deepcopy(defaults)
    for key, value in overrides.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings() -> dict:
    if not SETTINGS_FILE.exists():
        save_settings(copy.deepcopy(DEFAULT_SETTINGS))
        return copy.deepcopy(DEFAULT_SETTINGS)

    with open(SETTINGS_FILE, "r", encoding="utf-8") as file:
        raw_settings = json.load(file)

    settings = _deep_merge(DEFAULT_SETTINGS, raw_settings)
    get_sessions_dir(settings)
    get_summaries_dir(settings)
    return settings


def save_settings(data: dict):
    normalized = _deep_merge(DEFAULT_SETTINGS, data)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as file:
        json.dump(normalized, file, ensure_ascii=False, indent=2)

    get_sessions_dir(normalized)
    get_summaries_dir(normalized)


def _resolve_dir(raw_path: str | None, fallback: str) -> Path:
    path_value = (raw_path or fallback).strip()
    path = Path(path_value)
    if not path.is_absolute():
        path = BASE_DIR / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_sessions_dir(settings: dict | None = None) -> Path:
    current = settings or load_settings()
    return _resolve_dir(current.get("sessions_dir"), "sessions")


def get_summaries_dir(settings: dict | None = None) -> Path:
    current = settings or load_settings()
    return _resolve_dir(
        current.get("summaries_dir") or current.get("sessions_dir"),
        "sessions",
    )


def get_open_vacancy_keys(settings: dict | None = None) -> list[str]:
    current = settings or load_settings()
    presets = current.get("vacancy_presets", {})
    configured = current.get("open_vacancy_keys") or list(presets.keys())
    result: list[str] = []
    for key in configured:
        key_text = str(key).strip()
        if key_text and key_text in presets and key_text not in result:
            result.append(key_text)
    return result or list(presets.keys())


def get_vacancy_snapshot(
    settings: dict | None = None,
    key: str | None = None,
) -> dict:
    current = settings or load_settings()
    presets = current.get("vacancy_presets", {})
    active_key = str(current.get("active_vacancy_preset", "technical")).strip() or "technical"
    target_key = str(key or active_key).strip() or active_key
    if target_key not in presets:
        target_key = active_key if active_key in presets else next(iter(presets.keys()), "technical")

    preset = copy.deepcopy(presets.get(target_key, {}))
    if target_key == active_key:
        live_vacancy = current.get("vacancy", {})
        preset["title"] = live_vacancy.get("title", preset.get("title", "Vacancy"))
        preset["description"] = live_vacancy.get("description", preset.get("description", ""))
        preset["required_skills"] = copy.deepcopy(
            live_vacancy.get("required_skills", preset.get("required_skills", []))
        )
        preset["score_threshold"] = int(
            live_vacancy.get("score_threshold", preset.get("score_threshold", 28))
        )

    return {
        "key": target_key,
        "label": preset.get("label", target_key),
        "title": preset.get("title", "Vacancy"),
        "description": preset.get("description", ""),
        "required_skills": copy.deepcopy(preset.get("required_skills", [])),
        "score_threshold": int(preset.get("score_threshold", 28)),
    }


def list_open_vacancies(settings: dict | None = None) -> list[dict]:
    current = settings or load_settings()
    return [get_vacancy_snapshot(current, key) for key in get_open_vacancy_keys(current)]
