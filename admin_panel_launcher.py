from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BASE_DIR / "settings.json"
RUNTIME_DIR = BASE_DIR / "runtime"
ADMIN_URL_FILE = RUNTIME_DIR / "admin_url.txt"
WAIT_TIMEOUT_SEC = 60


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 1

    command = argv[1].strip().lower()
    if command == "detect":
        url = _find_running_url()
        if url:
            print(url)
            return 0
        return 1

    if command == "wait-open":
        return _wait_and_open_panel()

    return 1


def _wait_and_open_panel() -> int:
    deadline = time.time() + WAIT_TIMEOUT_SEC
    while time.time() < deadline:
        url = _read_runtime_url()
        if url and _healthcheck(url):
            webbrowser.open(url)
            print(url)
            return 0
        time.sleep(1)
    return 0


def _find_running_url() -> str | None:
    candidates: list[str] = []

    runtime_url = _read_runtime_url()
    if runtime_url:
        candidates.append(runtime_url)

    settings_url = _settings_admin_url()
    if settings_url and settings_url not in candidates:
        candidates.append(settings_url)

    for candidate in candidates:
        if _healthcheck(candidate):
            return candidate
    return None


def _read_runtime_url() -> str | None:
    try:
        value = ADMIN_URL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _settings_admin_url() -> str | None:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    host = str(data.get("admin_host", "127.0.0.1")).strip() or "127.0.0.1"
    if host in {"0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"

    try:
        port = int(data.get("admin_port", 8080))
    except (TypeError, ValueError):
        port = 8080

    return f"http://{host}:{port}"


def _healthcheck(url: str) -> bool:
    health_url = url.rstrip("/") + "/health"
    request = urllib.request.Request(
        health_url,
        headers={"User-Agent": "hr-bot-launcher"},
    )
    try:
        with urllib.request.urlopen(request, timeout=1.5) as response:
            body = response.read().decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return False

    compact = "".join(body.split())
    return compact == '{"status":"ok"}'


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
