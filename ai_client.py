from __future__ import annotations

import json
import logging
import time

import aiohttp

from config import get_vacancy_snapshot, load_settings

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
FREE_MODEL_CACHE_TTL_SEC = 15 * 60

DEFAULT_WORKING_FREE_MODELS = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter/free",
    "google/gemma-3-12b-it:free",
    "google/gemma-3-4b-it:free",
]

_live_free_models_cache: dict[str, object] = {
    "expires_at": 0.0,
    "models": [],
}
_model_backoff_until: dict[str, float] = {}
_model_backoff_reason: dict[str, str] = {}


async def chat_completion(
    messages: list[dict],
    model: str | None = None,
    *,
    purpose: str = "generic",
    temperature: float = 0.3,
    max_tokens: int = 1024,
) -> str:
    settings = load_settings()
    api_key = settings.get("openrouter_api_key", "").strip()
    if not api_key:
        raise RuntimeError("OpenRouter API key is not configured.")

    models_to_try = await _resolve_models(settings, api_key, model)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://hr-bot.local",
        "X-Title": "HR Telegram Bot",
    }
    timeout = aiohttp.ClientTimeout(total=90)

    logger.info(
        "OpenRouter request started purpose=%s candidate_models=%s",
        purpose,
        ", ".join(models_to_try[:8]),
    )

    failures: list[str] = []
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt, current_model in enumerate(models_to_try, start=1):
            cooldown_remaining = _cooldown_remaining(current_model)
            if cooldown_remaining > 0:
                logger.info(
                    "OpenRouter skip purpose=%s model=%s cooldown_remaining=%ss reason=%s",
                    purpose,
                    current_model,
                    cooldown_remaining,
                    _model_backoff_reason.get(current_model, "unknown"),
                )
                continue

            payload = {
                "model": current_model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            started_at = time.perf_counter()

            try:
                async with session.post(
                    OPENROUTER_URL,
                    headers=headers,
                    json=payload,
                ) as response:
                    raw_body = await response.text()
                    latency = round(time.perf_counter() - started_at, 2)

                    if response.status != 200:
                        detail = _extract_error_detail(raw_body)
                        cooldown = _cooldown_for_failure(response.status, detail)
                        _set_model_backoff(current_model, cooldown, detail)
                        logger.warning(
                            "OpenRouter failure purpose=%s attempt=%s/%s model=%s status=%s latency=%ss detail=%s",
                            purpose,
                            attempt,
                            len(models_to_try),
                            current_model,
                            response.status,
                            latency,
                            detail,
                        )
                        failures.append(
                            f"{current_model}: HTTP {response.status} ({detail})"
                        )
                        continue

                    data = json.loads(raw_body)
                    content = _extract_message_text(data)
                    if not content:
                        detail = _describe_empty_response(data)
                        cooldown = _cooldown_for_failure("empty", detail)
                        _set_model_backoff(current_model, cooldown, detail)
                        logger.warning(
                            "OpenRouter empty response purpose=%s attempt=%s/%s model=%s latency=%ss detail=%s",
                            purpose,
                            attempt,
                            len(models_to_try),
                            current_model,
                            latency,
                            detail,
                        )
                        failures.append(
                            f"{current_model}: empty response ({detail})"
                        )
                        continue

                    _clear_model_backoff(current_model)
                    logger.info(
                        "OpenRouter success purpose=%s attempt=%s/%s model=%s latency=%ss chars=%s",
                        purpose,
                        attempt,
                        len(models_to_try),
                        current_model,
                        latency,
                        len(content),
                    )
                    return content
            except Exception as error:
                latency = round(time.perf_counter() - started_at, 2)
                detail = f"{type(error).__name__}: {error}"
                cooldown = _cooldown_for_failure("exception", detail)
                _set_model_backoff(current_model, cooldown, detail)
                logger.warning(
                    "OpenRouter exception purpose=%s attempt=%s/%s model=%s latency=%ss detail=%s",
                    purpose,
                    attempt,
                    len(models_to_try),
                    current_model,
                    latency,
                    detail,
                )
                failures.append(f"{current_model}: {detail}")

    summary = " | ".join(failures[:4]) if failures else "no candidate models available"
    raise RuntimeError(
        f"All candidate models failed for {purpose}. {summary}"
    )


async def check_off_topic(message_text: str) -> bool:
    settings = load_settings()
    prompt = settings["prompts"]["off_topic_check"].format(message=message_text)
    try:
        result = await chat_completion(
            [{"role": "user", "content": prompt}],
            purpose="off_topic_check",
            temperature=0.0,
            max_tokens=12,
        )
        return result.strip().upper().startswith("NO")
    except Exception as error:
        logger.error("Off-topic check failed after fallbacks: %s", error)
        return False


async def analyze_resume(resume_text: str, vacancy: dict | None = None) -> str:
    settings = load_settings()
    vacancy = vacancy or get_vacancy_snapshot(settings)
    prompt = settings["prompts"]["resume_analysis"].format(
        title=vacancy["title"],
        description=vacancy["description"],
        skills=", ".join(vacancy["required_skills"]),
        resume=resume_text,
    )
    return await chat_completion(
        [{"role": "user", "content": prompt}],
        purpose="resume_analysis",
        temperature=0.2,
        max_tokens=700,
    )


async def screen_resume(resume_text: str, vacancy: dict | None = None) -> dict:
    settings = load_settings()
    vacancy = vacancy or get_vacancy_snapshot(settings)
    system_prompt = (
        "You are an HR screening assistant. "
        "Return only valid JSON and nothing else."
    )
    user_prompt = (
        f"Vacancy title: {vacancy['title']}\n"
        f"Vacancy description: {vacancy['description']}\n"
        f"Required skills: {', '.join(vacancy['required_skills'])}\n\n"
        f"Candidate resume:\n{resume_text}\n\n"
        "Task:\n"
        "1. Decide whether the candidate's profession or direction clearly matches the vacancy.\n"
        "2. Reject only if the profession/direction is clearly different or the resume is obviously irrelevant.\n"
        "3. Minor skill gaps are not a reason to reject.\n\n"
        "Return valid JSON with exactly these keys:\n"
        "{\n"
        '  "fit_score": integer 0-10,\n'
        '  "profession_match": true/false,\n'
        '  "should_reject": true/false,\n'
        '  "candidate_message": "short Russian message for the candidate",\n'
        '  "employer_summary": "short Russian summary for the employer",\n'
        '  "key_matches": ["short item"],\n'
        '  "key_gaps": ["short item"],\n'
        '  "missing_information": ["what is missing in the resume and must be clarified in interview"]\n'
        "}"
    )
    raw = await chat_completion(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        purpose="resume_screening",
        temperature=0.0,
        max_tokens=500,
    )
    return _normalize_resume_screening(raw, vacancy, resume_text)


async def assess_interview_answer(
    *,
    topic_name: str,
    topic_goal: str,
    question_text: str,
    answer_text: str,
) -> dict:
    system_prompt = (
        "You evaluate one hiring interview answer. "
        "Return only valid JSON and nothing else."
    )
    user_prompt = (
        f"Interview topic: {topic_name}\n"
        f"Goal of the topic: {topic_goal}\n"
        f"Question asked: {question_text}\n"
        f"Candidate answer: {answer_text}\n\n"
        "Return valid JSON with exactly these keys:\n"
        "{\n"
        '  "relevant": true/false,\n'
        '  "sufficient": true/false,\n'
        '  "follow_up_needed": true/false,\n'
        '  "suggested_follow_up_question": "one short Russian question or empty string",\n'
        '  "short_reason": "short Russian phrase",\n'
        '  "extracted_facts": ["short fact in Russian"],\n'
        '  "missing_points": ["short missing point in Russian"]\n'
        "}\n"
        "Mark sufficient=false when the answer is too vague, too short, evasive, or misses the requested concrete details. "
        "Set follow_up_needed=true only if one more уточняющий вопрос materially helps clarify the same topic."
    )
    raw = await chat_completion(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        purpose="interview_answer_assessment",
        temperature=0.0,
        max_tokens=260,
    )
    return _normalize_interview_assessment(raw, answer_text)


async def score_candidate(
    *,
    vacancy: dict,
    resume_text: str,
    screening: dict | None = None,
    interview_notes: list[dict] | None = None,
    dialog: list[dict] | None = None,
) -> dict:
    screening = screening or {}
    interview_notes = interview_notes or []
    dialog = dialog or []
    dialog_text = "\n".join(
        f"{'Candidate' if item.get('role') == 'user' else 'HR'}: {item.get('content', '')}"
        for item in dialog
    )
    notes_text = "\n".join(
        (
            f"{index}. Topic: {item.get('topic_name') or item.get('gap_label') or '-'}\n"
            f"Question: {item.get('question', '-')}\n"
            f"Answer: {item.get('answer', '-')}\n"
            f"Facts: {'; '.join(item.get('facts', [])) if item.get('facts') else '-'}\n"
            f"Gaps: {'; '.join(item.get('missing_points', [])) if item.get('missing_points') else '-'}"
        )
        for index, item in enumerate(interview_notes, start=1)
    )
    prompt = (
        "You are a senior recruiter evaluating one candidate after resume screening and interview. "
        "Return only valid JSON and nothing else.\n\n"
        f"Vacancy title: {vacancy.get('title', '')}\n"
        f"Vacancy description: {vacancy.get('description', '')}\n"
        f"Required skills: {', '.join(vacancy.get('required_skills', []))}\n"
        f"Threshold score: {int(vacancy.get('score_threshold', 28))}/40\n\n"
        f"Resume:\n{resume_text}\n\n"
        f"Resume screening summary:\n{json.dumps(screening, ensure_ascii=False)}\n\n"
        f"Interview notes:\n{notes_text or '-'}\n\n"
        f"Dialogue excerpt:\n{dialog_text or '-'}\n\n"
        "Return JSON with exactly these keys:\n"
        "{\n"
        '  "experience_score": integer 1-10,\n'
        '  "skills_score": integer 1-10,\n'
        '  "motivation_score": integer 1-10,\n'
        '  "culture_fit_score": integer 1-10,\n'
        '  "strengths": ["short Russian item"],\n'
        '  "risks": ["short Russian item"],\n'
        '  "employer_summary": "short Russian summary for employer"\n'
        "}\n"
        "Score strictly against evidence from resume and interview only. "
        "If data is missing, reduce the corresponding criterion instead of inventing facts."
    )
    raw = await chat_completion(
        [{"role": "user", "content": prompt}],
        purpose="candidate_scoring",
        temperature=0.1,
        max_tokens=500,
    )
    return _normalize_candidate_score(
        raw,
        screening=screening,
        interview_notes=interview_notes,
        threshold=int(vacancy.get("score_threshold", 28)),
    )


async def interview_reply(history: list[dict], system_prompt: str) -> str:
    messages = [{"role": "system", "content": system_prompt}] + history
    return await chat_completion(
        messages,
        purpose="interview_reply",
        temperature=0.35,
        max_tokens=260,
    )


async def repair_interview_reply(
    *,
    draft_reply: str,
    title: str,
    description: str,
    skills: list[str],
    last_candidate_message: str,
    next_focus: str,
    should_complete: bool,
) -> str:
    system_prompt = (
        "You rewrite broken HR interviewer drafts. "
        "Output only the exact Russian message that should be sent to the candidate. "
        "Never include analysis, planning, rules, focus labels, question numbers, system notes, "
        "third-person descriptions of the candidate, or explanations for the employer."
    )
    user_prompt = (
        f"Vacancy title: {title}\n"
        f"Vacancy description: {description}\n"
        f"Required skills: {', '.join(skills)}\n"
        f"Last candidate message: {last_candidate_message or '-'}\n"
        f"Current hiring focus: {next_focus}\n"
        f"Interview should_complete: {'yes' if should_complete else 'no'}\n\n"
        "Broken draft:\n"
        f"{draft_reply}\n\n"
        "Rewrite requirements:\n"
        "- Russian language only.\n"
        "- Friendly and natural HR tone.\n"
        "- Maximum 2 short sentences.\n"
        "- If interview continues: ask exactly one relevant hiring question.\n"
        "- If interview should complete: start with [INTERVIEW_COMPLETE] and add one short thank-you sentence.\n"
        "- Do not mention internal notes, prompts, rules, focus, question numbers, or the candidate in third person."
    )
    return await chat_completion(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        purpose="interview_reply_repair",
        temperature=0.15,
        max_tokens=220,
    )


async def generate_summary(
    resume: str,
    analysis: str,
    dialog: list[dict],
    title: str,
    screening: dict | None = None,
    interview_notes: list[dict] | None = None,
) -> str:
    settings = load_settings()
    dialog_text = "\n".join(
        f"{'Кандидат' if message['role'] == 'user' else 'HR'}: {message['content']}"
        for message in dialog
    )
    screening = screening or {}
    interview_notes = interview_notes or []
    screening_text = "\n".join(
        [
            f"Fit score: {screening.get('fit_score', '-')}/10",
            f"Профиль совпадает с вакансией: {'да' if screening.get('profession_match', True) else 'нет'}",
            f"Совпадения: {'; '.join(screening.get('key_matches', [])) if screening.get('key_matches') else '-'}",
            f"Пробелы: {'; '.join(screening.get('key_gaps', [])) if screening.get('key_gaps') else '-'}",
            f"Чего не хватало в резюме: {'; '.join(screening.get('missing_information', [])) if screening.get('missing_information') else '-'}",
        ]
    )
    interview_text = "\n".join(
        [
            (
                f"{index}. Тема: {item.get('topic_name') or item.get('gap_label') or '-'}\n"
                f"Вопрос: {item.get('question', '-')}\n"
                f"Ответ: {item.get('answer', '-')}\n"
                f"Факты: {'; '.join(item.get('facts', [])) if item.get('facts') else '-'}\n"
                f"Что осталось неясно: {'; '.join(item.get('missing_points', [])) if item.get('missing_points') else '-'}"
            )
            for index, item in enumerate(interview_notes, start=1)
        ]
    )
    base_prompt = settings["prompts"]["summary"].format(
        title=title,
        resume=resume,
        analysis=analysis,
        dialog=dialog_text,
    )
    prompt = (
        f"{base_prompt}\n\n"
        "Дополнительный контекст, который обязательно нужно учесть:\n"
        "1. Сделай акцент на том, какой информации не хватало в резюме.\n"
        "2. Покажи, что именно удалось выяснить на интервью.\n"
        "3. Отдельно перечисли, что после интервью все еще осталось невыясненным.\n\n"
        f"Скрининг резюме:\n{screening_text}\n\n"
        f"Уточняющие вопросы и ответы:\n{interview_text or '-'}"
    )
    return await chat_completion(
        [{"role": "user", "content": prompt}],
        purpose="summary_generation",
        temperature=0.2,
        max_tokens=1100,
    )


async def _resolve_models(
    settings: dict,
    api_key: str,
    model_override: str | None,
) -> list[str]:
    if model_override and model_override.strip().lower() != "auto":
        explicit = model_override.strip()
        if _is_rotatable_free_model(explicit):
            live_models = await _fetch_live_free_models(api_key)
            return _deduplicate(
                [explicit, *settings.get("openrouter_free_models", []), *DEFAULT_WORKING_FREE_MODELS, *live_models]
            )
        return [explicit]

    configured_model = settings.get("openrouter_model", "auto").strip()
    if configured_model and configured_model.lower() != "auto":
        if _is_rotatable_free_model(configured_model):
            live_models = await _fetch_live_free_models(api_key)
            return _deduplicate(
                [configured_model, *settings.get("openrouter_free_models", []), *DEFAULT_WORKING_FREE_MODELS, *live_models]
            )
        return [configured_model]

    live_models = await _fetch_live_free_models(api_key)
    candidates = [
        *settings.get("openrouter_free_models", []),
        *DEFAULT_WORKING_FREE_MODELS,
        *live_models,
    ]
    models = _deduplicate(
        model_name.strip()
        for model_name in candidates
        if model_name and str(model_name).strip()
    )
    if not models:
        raise RuntimeError("OpenRouter free models list is empty.")
    return models


async def _fetch_live_free_models(api_key: str) -> list[str]:
    now = time.time()
    expires_at = float(_live_free_models_cache.get("expires_at", 0))
    if now < expires_at:
        return list(_live_free_models_cache.get("models", []))

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=30)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(OPENROUTER_MODELS_URL, headers=headers) as response:
                if response.status != 200:
                    detail = await response.text()
                    logger.warning(
                        "OpenRouter models endpoint failed status=%s detail=%s",
                        response.status,
                        _trim(detail, 240),
                    )
                    return list(_live_free_models_cache.get("models", []))
                data = await response.json()
    except Exception as error:
        logger.warning("Failed to refresh OpenRouter free models list: %s", error)
        return list(_live_free_models_cache.get("models", []))

    models: list[str] = []
    for item in data.get("data", []):
        if not _looks_like_free_model(item):
            continue
        if not _supports_text_output(item):
            continue
        model_id = str(item.get("id", "")).strip()
        if model_id:
            models.append(model_id)

    ordered = _sort_models(models)
    _live_free_models_cache["models"] = ordered
    _live_free_models_cache["expires_at"] = now + FREE_MODEL_CACHE_TTL_SEC
    logger.info(
        "Refreshed OpenRouter free models list: %s models available",
        len(ordered),
    )
    return ordered


def _looks_like_free_model(item: dict) -> bool:
    model_id = str(item.get("id", "")).strip()
    if model_id == "openrouter/free":
        return True

    pricing = item.get("pricing") or {}
    prompt_price = str(pricing.get("prompt", ""))
    completion_price = str(pricing.get("completion", ""))
    return model_id.endswith(":free") or (
        prompt_price == "0" and completion_price == "0"
    )


def _supports_text_output(item: dict) -> bool:
    architecture = item.get("architecture") or {}
    modality = str(architecture.get("modality", "")).strip().lower()
    if "->" not in modality:
        return False
    output_modality = modality.split("->", 1)[1]
    return "text" in output_modality and "audio" not in output_modality


def _sort_models(models: list[str]) -> list[str]:
    preferred_order = {
        model_name: index
        for index, model_name in enumerate(DEFAULT_WORKING_FREE_MODELS)
    }
    return sorted(
        set(models),
        key=lambda model_name: (
            preferred_order.get(model_name, 10_000),
            model_name,
        ),
    )


def _extract_message_text(data: dict) -> str | None:
    choices = data.get("choices") or []
    if not choices:
        return None

    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        stripped = content.strip()
        return stripped or None

    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
                continue
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"text", "output_text"}:
                chunks.append(str(part.get("text", "")))
            elif "text" in part:
                chunks.append(str(part.get("text", "")))
        combined = "\n".join(chunk.strip() for chunk in chunks if chunk.strip()).strip()
        return combined or None

    return None


def _normalize_resume_screening(raw_text: str, vacancy: dict, resume_text: str) -> dict:
    parsed = _parse_json_object(raw_text)
    if not isinstance(parsed, dict):
        return _fallback_resume_screening(raw_text, vacancy, resume_text)

    fit_score = _safe_int(parsed.get("fit_score"), 5)
    profession_match = _as_bool(parsed.get("profession_match", True))
    should_reject = _as_bool(parsed.get("should_reject", False))
    employer_summary = str(parsed.get("employer_summary", "")).strip() or _trim(raw_text.strip(), 900)
    candidate_message = str(parsed.get("candidate_message", "")).strip()
    if not candidate_message:
        candidate_message = (
            f"Спасибо за отклик. Сейчас мы не продолжаем интервью, потому что профиль резюме "
            f"не совпадает с вакансией {vacancy['title']}."
            if should_reject or not profession_match
            else "Спасибо, резюме подходит по профилю. Перехожу к вопросам."
        )
    key_gaps = _normalize_string_list(parsed.get("key_gaps"))
    missing_information = _normalize_string_list(parsed.get("missing_information"))
    if not missing_information:
        missing_information = _derive_missing_information(
            resume_text,
            key_gaps,
            vacancy.get("required_skills", []),
        )
    return {
        "fit_score": max(0, min(10, fit_score)),
        "profession_match": profession_match,
        "should_reject": should_reject or not profession_match,
        "candidate_message": candidate_message,
        "employer_summary": employer_summary,
        "key_matches": _normalize_string_list(parsed.get("key_matches")),
        "key_gaps": key_gaps,
        "missing_information": missing_information,
    }


def _fallback_resume_screening(raw_text: str, vacancy: dict, resume_text: str) -> dict:
    lowered = raw_text.lower()
    mismatch_markers = (
        "не соответствует вакансии",
        "не подходит по профилю",
        "нерелевант",
        "другая профессия",
        "другая специальность",
        "не совпадает с вакансией",
    )
    should_reject = any(marker in lowered for marker in mismatch_markers)
    key_gaps = []
    missing_information = _derive_missing_information(
        resume_text,
        key_gaps,
        vacancy.get("required_skills", []),
    )
    return {
        "fit_score": 2 if should_reject else 6,
        "profession_match": not should_reject,
        "should_reject": should_reject,
        "candidate_message": (
            f"Спасибо за отклик. Сейчас мы не продолжаем интервью, потому что профиль резюме "
            f"не совпадает с вакансией {vacancy['title']}."
            if should_reject
            else "Спасибо, резюме выглядит релевантно. Перехожу к вопросам."
        ),
        "employer_summary": _trim(raw_text.strip(), 900),
        "key_matches": [],
        "key_gaps": key_gaps,
        "missing_information": missing_information,
    }


def _normalize_interview_assessment(raw_text: str, answer_text: str) -> dict:
    parsed = _parse_json_object(raw_text)
    if not isinstance(parsed, dict):
        return _fallback_interview_assessment(answer_text)

    return {
        "relevant": _as_bool(parsed.get("relevant", True)),
        "sufficient": _as_bool(parsed.get("sufficient", False)),
        "follow_up_needed": _as_bool(parsed.get("follow_up_needed", False)),
        "suggested_follow_up_question": str(
            parsed.get("suggested_follow_up_question", "")
        ).strip(),
        "short_reason": str(parsed.get("short_reason", "")).strip() or "Оценка ответа",
        "extracted_facts": _normalize_string_list(parsed.get("extracted_facts")),
        "missing_points": _normalize_string_list(parsed.get("missing_points")),
    }


def _fallback_interview_assessment(answer_text: str) -> dict:
    normalized = " ".join(answer_text.strip().split())
    sufficient = len(normalized) >= 45
    return {
        "relevant": True,
        "sufficient": sufficient,
        "follow_up_needed": not sufficient,
        "suggested_follow_up_question": "",
        "short_reason": "Ответ слишком общий" if not sufficient else "Ответ принят",
        "extracted_facts": [normalized] if normalized else [],
        "missing_points": (
            ["Нужен более конкретный пример, стек и результат"]
            if not sufficient else []
        ),
    }


def _normalize_candidate_score(
    raw_text: str,
    *,
    screening: dict,
    interview_notes: list[dict],
    threshold: int,
) -> dict:
    parsed = _parse_json_object(raw_text)
    if not isinstance(parsed, dict):
        return _fallback_candidate_score(
            screening=screening,
            interview_notes=interview_notes,
            threshold=threshold,
        )

    experience = max(1, min(10, _safe_int(parsed.get("experience_score"), 5)))
    skills = max(1, min(10, _safe_int(parsed.get("skills_score"), 5)))
    motivation = max(1, min(10, _safe_int(parsed.get("motivation_score"), 5)))
    culture_fit = max(1, min(10, _safe_int(parsed.get("culture_fit_score"), 5)))
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
        "strengths": _normalize_string_list(parsed.get("strengths")),
        "risks": _normalize_string_list(parsed.get("risks")),
        "employer_summary": str(parsed.get("employer_summary", "")).strip(),
    }


def _fallback_candidate_score(
    *,
    screening: dict,
    interview_notes: list[dict],
    threshold: int,
) -> dict:
    fit_score = max(1, min(10, _safe_int(screening.get("fit_score"), 5)))
    sufficient_answers = sum(1 for item in interview_notes if item.get("sufficient"))
    note_count = max(1, len(interview_notes))
    coverage_bonus = min(2, sufficient_answers // 2)
    experience = max(1, min(10, fit_score + coverage_bonus))
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
        "strengths": _normalize_string_list(screening.get("key_matches")),
        "risks": _normalize_string_list(screening.get("key_gaps")),
        "employer_summary": "",
    }


def _parse_json_object(raw_text: str):
    text = raw_text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    candidate = text[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _normalize_string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            items.append(text)
    return items[:8]


def _derive_missing_information(
    resume_text: str,
    key_gaps: list[str],
    required_skills: list[str],
) -> list[str]:
    resume_lower = resume_text.lower()
    items: list[str] = []

    for gap in key_gaps:
        lowered = gap.lower()
        if any(marker in lowered for marker in ("не совпадает с вакансией", "другая профессия")):
            continue
        items.append(gap)

    if not _contains_any(resume_lower, ["проект", "задач", "достижен", "результат", "role", "роль"]):
        items.append("Конкретный пример последнего релевантного проекта и личной роли")
    if not _contains_any(resume_lower, ["зарплат", "salary", "оклад", "ожидан"]):
        items.append("Зарплатные ожидания")
    if not _contains_any(resume_lower, ["удален", "удалён", "гибрид", "офис", "формат работы"]):
        items.append("Предпочитаемый формат работы")
    if not _contains_any(resume_lower, ["смогу выйти", "готов выйти", "дата выхода", "notice", "срок выхода"]):
        items.append("Срок выхода на работу")

    for skill in required_skills[:3]:
        skill_text = str(skill).strip()
        if skill_text and skill_text.lower() not in resume_lower:
            items.append(f"Подтвержденный практический опыт с {skill_text}")

    return _deduplicate(items)[:8]


def _contains_any(text: str, fragments: list[str]) -> bool:
    return any(fragment in text for fragment in fragments)


def _describe_empty_response(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        return "choices list is empty"

    choice = choices[0]
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason")
    has_reasoning = bool(message.get("reasoning"))
    model_name = data.get("model", "unknown")
    return (
        f"model={model_name}, finish_reason={finish_reason}, "
        f"content_missing=True, reasoning_present={has_reasoning}"
    )


def _extract_error_detail(raw_body: str) -> str:
    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError:
        return _trim(raw_body, 240)

    error = data.get("error") or {}
    message = str(error.get("message", "")).strip()
    metadata = error.get("metadata") or {}
    raw = str(metadata.get("raw", "")).strip()
    parts = [part for part in (message, raw) if part]
    if parts:
        return _trim(" | ".join(parts), 240)
    return _trim(raw_body, 240)


def _cooldown_for_failure(status: int | str, detail: str) -> int:
    detail_lower = detail.lower()
    if status == 404:
        return 60 * 60
    if status == 429:
        return 3 * 60
    if status in {502, 503, 504}:
        return 2 * 60
    if status == "empty":
        return 10 * 60 if "reasoning_present=true" in detail_lower else 3 * 60
    if status == "exception":
        return 60
    return 2 * 60


def _set_model_backoff(model_name: str, seconds: int, reason: str):
    _model_backoff_until[model_name] = time.time() + max(1, seconds)
    _model_backoff_reason[model_name] = _trim(reason, 240)


def _clear_model_backoff(model_name: str):
    _model_backoff_until.pop(model_name, None)
    _model_backoff_reason.pop(model_name, None)


def _cooldown_remaining(model_name: str) -> int:
    until = _model_backoff_until.get(model_name, 0)
    return max(0, int(until - time.time()))


def _is_rotatable_free_model(model_name: str) -> bool:
    return model_name == "openrouter/free" or model_name.endswith(":free")


def _deduplicate(items) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        normalized = str(item).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _safe_int(value, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return fallback


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "да"}


def _trim(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."
