import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from admin_web import start_admin_server
from app_logging import setup_logging, write_crash_log
from candidate import router as candidate_router
from config import load_settings
from employer import router as employer_router

setup_logging()
logger = logging.getLogger(__name__)


async def main():
    settings = load_settings()
    telegram_token = settings.get("telegram_token", "").strip()
    if not telegram_token:
        raise RuntimeError("Telegram token is not configured in settings.json")

    bot = Bot(token=telegram_token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(employer_router)
    dispatcher.include_router(candidate_router)

    admin_runner, admin_url = await start_admin_server(bot)
    allowed_updates = dispatcher.resolve_used_update_types()
    logger.info("startup event=admin_panel_ready url=%s", admin_url)
    logger.info(
        "startup event=telegram_polling_started allowed_updates=%s",
        ",".join(allowed_updates),
    )
    try:
        await dispatcher.start_polling(
            bot,
            allowed_updates=allowed_updates,
        )
    finally:
        logger.info("shutdown event=cleanup_started")
        await admin_runner.cleanup()
        await bot.session.close()
        logger.info("shutdown event=cleanup_finished")


def run() -> int:
    try:
        asyncio.run(main())
        return 0
    except KeyboardInterrupt:
        logger.info("Application stopped by user.")
        return 0
    except Exception as error:
        crash_path = write_crash_log(
            error,
            context={
                "entrypoint": "main.run",
                "hint": (
                    "Если это ошибка порта, закройте второй экземпляр приложения "
                    "или смените admin_port в settings.json."
                ),
            },
        )
        logger.critical("Application crashed. Crash log saved to %s", crash_path)
        print()
        print("[CRASH] Application stopped unexpectedly.")
        print(f"[CRASH] Reason: {error}")
        print(f"[CRASH] Crash log saved to: {crash_path}")
        print()
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
