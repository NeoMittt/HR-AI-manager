from __future__ import annotations

import json
import logging
import platform
import sys
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
APP_LOG_FILE = BASE_DIR / "app.log"
CRASH_LOG_DIR = BASE_DIR / "crash_logs"
CRASH_LOG_DIR.mkdir(exist_ok=True)

NOISY_LOGGER_NAMES = {
    "aiohttp.access",
    "aiogram.event",
}
NOISY_MESSAGE_PARTS = (
    "GET /api/logs",
    "GET /health",
)


class _NoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name in NOISY_LOGGER_NAMES:
            return False

        message = record.getMessage()
        return not any(part in message for part in NOISY_MESSAGE_PARTS)


def _is_noise_line(line: str) -> bool:
    return any(
        part in line
        for part in (
            "aiohttp.access",
            "aiogram.event",
            "GET /api/logs",
            "GET /health",
        )
    )


def setup_logging() -> Path:
    root_logger = logging.getLogger()
    if getattr(root_logger, "_hr_bot_logging_ready", False):
        return APP_LOG_FILE

    root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(_NoiseFilter())
    root_logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        APP_LOG_FILE,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(_NoiseFilter())
    root_logger.addHandler(file_handler)

    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    root_logger._hr_bot_logging_ready = True
    return APP_LOG_FILE


def read_log_tail(max_lines: int = 200) -> str:
    if not APP_LOG_FILE.exists():
        return "Логов пока нет."

    try:
        with open(APP_LOG_FILE, "r", encoding="utf-8", errors="replace") as file:
            lines = file.readlines()
    except OSError as error:
        return f"Не удалось прочитать лог: {error}"

    filtered = [
        line
        for line in lines
        if not _is_noise_line(line)
    ]
    tail = filtered[-max_lines:]
    return "".join(tail).strip() or "Логов пока нет."


def write_crash_log(
    error: BaseException,
    *,
    context: dict | None = None,
) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = CRASH_LOG_DIR / f"crash_{timestamp}.log"

    payload = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "python": sys.version,
        "platform": platform.platform(),
        "cwd": str(BASE_DIR),
        "error_type": type(error).__name__,
        "error_message": str(error),
        "context": context or {},
        "app_log_tail": read_log_tail(80),
        "traceback": "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        ),
    }

    with open(path, "w", encoding="utf-8") as file:
        file.write("=== HR BOT CRASH LOG ===\n")
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")

    return path
