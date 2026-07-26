"""通知推送(企业微信群机器人,同步 httpx,供 worker/哨兵使用)。

notify() 是统一入口,后续新增通道(飞书/邮件等)在这里扩展。
"""

import httpx

from app.config import get_settings
from app.logger import get_logger

logger = get_logger(__name__)

_TIMEOUT = 10.0


def send_wecom_message(text: str) -> bool:
    """POST settings.wecom_webhook_url(企微群机器人)。

    未配置/网络异常/非 2xx/响应 errcode!=0 均返回 False(成功时 HTTP 200 且 errcode=0)。
    不抛异常:哨兵/管道不因通知失败而重试。
    """
    settings = get_settings()
    url = settings.wecom_webhook_url
    if not url:
        logger.warning("wecom_not_configured")
        return False

    payload = {"msgtype": "text", "text": {"content": text}}
    try:
        resp = httpx.post(url, json=payload, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        logger.warning("wecom_send_error", error=str(exc))
        return False

    if not resp.is_success:
        logger.warning(
            "wecom_send_non_2xx", status_code=resp.status_code, body=resp.text[:200]
        )
        return False

    try:
        errcode = resp.json().get("errcode")
    except ValueError:
        errcode = None
    if errcode != 0:
        logger.warning("wecom_send_errcode_nonzero", errcode=errcode, body=resp.text[:200])
        return False
    return True


def notify(text: str) -> bool:
    """统一通知入口:当前走企业微信;未配置 warning 并返回 False。"""
    if not get_settings().wecom_webhook_url:
        logger.warning("notify_no_channel_configured")
        return False
    return send_wecom_message(text)
