"""Bot 进程入口:python -m app.bot.runner(长轮询)。"""

import asyncio

from aiogram import Bot, Dispatcher

from app.bot.handlers import router
from app.config import get_settings
from app.logger import get_logger, setup_logging

logger = get_logger(__name__)


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    if not settings.telegram_bot_token:
        raise RuntimeError("telegram_bot_token 未配置,无法启动 Bot")
    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    logger.info("bot_polling_start")
    asyncio.run(dp.start_polling(bot))


if __name__ == "__main__":
    main()
