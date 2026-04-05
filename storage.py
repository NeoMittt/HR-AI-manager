from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

import aiofiles

from config import get_sessions_dir, get_summaries_dir

logger = logging.getLogger(__name__)
MAX_SESSION_EVENTS = 120


def session_path(user_id: int) -> Path:
    return get_sessions_dir() / f"{user_id}.json"


def session_exists(user_id: int) -> bool:
    return session_path(user_id).exists()


def summary_path(user_id: int, username: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = _slugify(username or str(user_id))
    return get_summaries_dir() / f"summary_{safe_name}_{ts}.txt"


def create_session(user_id: int, username: str | None = None) -> dict:
    session = _empty_session(user_id)
    session["username"] = username
    return session


async def load_session(user_id: int) -> dict:
    path = session_path(user_id)
    if not path.exists():
        return create_session(user_id)

    try:
        async with aiofiles.open(path, "r", encoding="utf-8") as file:
            data = await file.read()
        if not data.strip():
            raise ValueError("empty session file")
        loaded = json.loads(data)
    except Exception as error:
        logger.error("Session load error for %s: %s", user_id, error)
        _quarantine_broken_session_file(path, user_id)
        return create_session(user_id)

    session = create_session(user_id)
    session.update(loaded)
    session.setdefault("interview_history", [])
    session.setdefault("session_events", [])
    session.setdefault("interview_topic_index", 0)
    session.setdefault("interview_followup_used", False)
    session.setdefault("interview_topics", [])
    session.setdefault("interview_notes", [])
    session.setdefault("resume_screening", None)
    session.setdefault("candidate_score", None)
    session.setdefault("employer_decision", "pending")
    session.setdefault("permanent_block", False)
    session.setdefault("block_reason", None)
    session.setdefault("last_question_text", None)
    session.setdefault("last_question_topic", None)
    session.setdefault("last_question_mode", None)
    session.setdefault("vacancy_key", None)
    session.setdefault("vacancy", None)
    session.setdefault("awaiting_vacancy_choice", False)
    session.setdefault("awaiting_repeat_choice", False)
    session.setdefault("round_number", 1)
    session.setdefault("round_history_start_index", 0)
    return session


async def save_session(session: dict):
    session["updated_at"] = datetime.now().isoformat()
    path = session_path(session["user_id"])
    payload = json.dumps(
        _json_safe(session),
        ensure_ascii=False,
        indent=2,
    )
    temp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        async with aiofiles.open(temp_path, "w", encoding="utf-8") as file:
            await file.write(payload)
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


async def delete_session(user_id: int):
    path = session_path(user_id)
    if path.exists():
        path.unlink()


async def save_summary(user_id: int, username: str, summary_text: str) -> Path:
    path = summary_path(user_id, username)
    async with aiofiles.open(path, "w", encoding="utf-8") as file:
        header = (
            "=== СВОДКА КАНДИДАТА ===\n"
            f"ID: {user_id}\n"
            f"Имя: {username or '—'}\n"
            f"Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
            f"{'=' * 40}\n\n"
        )
        await file.write(header + summary_text)
    return path


def add_dialog_message(
    session: dict,
    role: str,
    content: str,
    *,
    source: str = "ai",
):
    session.setdefault("interview_history", []).append(
        {
            "role": role,
            "content": content,
            "source": source,
            "timestamp": datetime.now().isoformat(),
        }
    )


def add_session_event(
    session: dict,
    actor: str,
    event: str,
    **details,
):
    session.setdefault("session_events", []).append(
        {
            "at": datetime.now().isoformat(),
            "actor": actor,
            "event": event,
            "details": _json_safe(details),
        }
    )
    session["session_events"] = session["session_events"][-MAX_SESSION_EVENTS:]


def list_sessions() -> list[dict]:
    sessions = []
    for path in get_sessions_dir().glob("*.json"):
        try:
            user_id = int(path.stem)
        except ValueError:
            continue
        data = get_session_snapshot(user_id)
        if data:
            sessions.append(data)
    return sorted(
        sessions,
        key=lambda item: item.get("updated_at") or item.get("started_at") or "",
        reverse=True,
    )


def get_session_snapshot(user_id: int) -> dict | None:
    path = session_path(user_id)
    if not path.exists():
        return None

    try:
        with open(path, "r", encoding="utf-8") as file:
            if path.stat().st_size == 0:
                raise ValueError("empty session file")
            data = json.load(file)
    except Exception as error:
        logger.error("Session snapshot error for %s: %s", user_id, error)
        _quarantine_broken_session_file(path, user_id)
        return None

    started_at = data.get("started_at", "—")
    return {
        "user_id": data.get("user_id", user_id),
        "username": data.get("username", "—"),
        "state": data.get("state", "unknown"),
        "return_state": data.get("return_state"),
        "started_at": started_at,
        "updated_at": data.get("updated_at", started_at),
        "resume_received": bool(data.get("resume_text")),
        "resume_text": data.get("resume_text"),
        "resume_analysis": data.get("resume_analysis"),
        "summary_saved": bool(data.get("summary_saved")),
        "summary_path": data.get("summary_path"),
        "off_topic_count": data.get("off_topic_count", 0),
        "block_until": data.get("block_until"),
        "permanent_block": bool(data.get("permanent_block")),
        "block_reason": data.get("block_reason"),
        "resume_screening": data.get("resume_screening"),
        "candidate_score": data.get("candidate_score"),
        "employer_decision": data.get("employer_decision", "pending"),
        "interview_notes": data.get("interview_notes", []),
        "interview_history": data.get("interview_history", []),
        "session_events": data.get("session_events", []),
        "vacancy_key": data.get("vacancy_key"),
        "vacancy": data.get("vacancy"),
        "awaiting_vacancy_choice": bool(data.get("awaiting_vacancy_choice")),
        "awaiting_repeat_choice": bool(data.get("awaiting_repeat_choice")),
        "round_number": data.get("round_number", 1),
        "round_history_start_index": data.get("round_history_start_index", 0),
    }


def list_summaries() -> list[Path]:
    return sorted(
        get_summaries_dir().glob("summary_*.txt"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", value.strip())
    return cleaned.strip("_") or "candidate"


def _json_safe(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def _quarantine_broken_session_file(path: Path, user_id: int):
    if not path.exists():
        return

    broken_name = (
        f"{path.stem}.broken_{datetime.now().strftime('%Y%m%d_%H%M%S')}{path.suffix}"
    )
    broken_path = path.with_name(broken_name)
    try:
        path.replace(broken_path)
        logger.warning(
            "Session file for %s was moved to %s after read failure.",
            user_id,
            broken_path.name,
        )
    except OSError as error:
        logger.warning(
            "Failed to quarantine broken session file for %s: %s",
            user_id,
            error,
        )


def _empty_session(user_id: int) -> dict:
    now = datetime.now().isoformat()
    return {
        "user_id": user_id,
        "username": None,
        "state": "waiting_resume",
        "return_state": None,
        "started_at": now,
        "updated_at": now,
        "resume_text": None,
        "resume_analysis": None,
        "interview_history": [],
        "session_events": [],
        "off_topic_count": 0,
        "block_until": None,
        "summary_saved": False,
        "summary_path": None,
        "interview_topic_index": 0,
        "interview_followup_used": False,
        "interview_notes": [],
        "resume_screening": None,
        "candidate_score": None,
        "employer_decision": "pending",
        "permanent_block": False,
        "block_reason": None,
        "last_question_text": None,
        "last_question_topic": None,
        "last_question_mode": None,
        "vacancy_key": None,
        "vacancy": None,
        "awaiting_vacancy_choice": False,
        "awaiting_repeat_choice": False,
        "round_number": 1,
        "round_history_start_index": 0,
    }
