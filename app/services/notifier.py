"""Telegram 推送(同步 httpx,供 worker/哨兵使用;Bot 交互在 app/bot)。"""

import httpx

from app.config import get_settings
from app.logger import get_logger

logger = get_logger(__name__)

_SEND_MESSAGE_URL = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT = 10.0


def send_telegram_message(
    text: str,
    chat_id: str | None = None,
    parse_mode: str | None = None,
) -> bool:
    """POST https://api.telegram.org/bot{token}/sendMessage。

    chat_id 缺省用 settings.telegram_alert_chat_id;
    token/chat_id 未配置时记日志并返回 False(不抛异常,哨兵不因通知失败而重试)。
    """
    settings = get_settings()
    token = settings.telegram_bot_token
    target_chat = chat_id or settings.telegram_alert_chat_id
    if not token or not target_chat:
        logger.warning(
            "telegram_not_configured", has_token=bool(token), has_chat_id=bool(target_chat)
        )
        return False

    payload: dict[str, str] = {"chat_id": target_chat, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        resp = httpx.post(
            _SEND_MESSAGE_URL.format(token=token), json=payload, timeout=_TIMEOUT
        )
    except httpx.HTTPError as exc:
        logger.warning("telegram_send_error", error=str(exc))
        return False

    if not resp.is_success:
        logger.warning(
            "telegram_send_non_2xx", status_code=resp.status_code, body=resp.text[:200]
        )
        return False
    return True
