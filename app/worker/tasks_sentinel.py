"""哨兵任务(Celery beat 定时):重复扣费检测。

scan_duplicate_charges(days=3):
1. firefly.list_transactions(近 days 天, withdrawal)
2. 按 (destination_name 归一化, amount) 分组,组内 >= 2 笔视为疑似重复扣费
3. 对每组生成告警文本(商户/金额/日期列表),notifier.send_telegram_message 推送
4. 返回 {"groups": 命中组数, "checked": 扫描笔数}
Firefly 不可达时记日志返回 {"error": ...},不抛异常(避免 beat 无限重试刷屏)。
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.logger import get_logger
from app.services.firefly_client import FireflyError, get_firefly_client
from app.services.notifier import send_telegram_message
from app.worker.celery_app import celery_app

logger = get_logger(__name__)


def _normalize_merchant(name: Any) -> str:
    """商户名归一化:去首尾空白 + 小写。"""
    return str(name or "").strip().lower()


def _normalize_amount(amount: Any) -> str:
    """金额归一化:Decimal 消除 "25.0"/"25.00" 差异;解析失败退回原始字符串。"""
    try:
        return str(Decimal(str(amount)).normalize())
    except (InvalidOperation, ValueError):
        return str(amount)


def _build_alert_text(splits: list[dict[str, Any]]) -> str:
    """单组告警文本:商户、金额、组内各笔日期。"""
    first = splits[0]
    merchant = str(first.get("destination_name") or "").strip() or "(未知商户)"
    amount = first.get("amount", "?")
    lines = [
        "疑似重复扣费提醒",
        f"商户:{merchant}",
        f"金额:{amount}",
        f"共 {len(splits)} 笔:",
    ]
    lines.extend(f"- {s.get('date', '未知日期')}" for s in splits)
    return "\n".join(lines)


@celery_app.task(name="app.worker.tasks_sentinel.scan_duplicate_charges")
def scan_duplicate_charges(days: int = 3) -> dict[str, Any]:
    end = date.today()
    start = end - timedelta(days=days)
    try:
        splits = get_firefly_client().list_transactions(start, end, txn_type="withdrawal")
    except (FireflyError, httpx.HTTPError) as exc:
        logger.warning("sentinel_firefly_unreachable", error=str(exc))
        return {"error": str(exc)}

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for split in splits:
        merchant = _normalize_merchant(split.get("destination_name"))
        if not merchant:
            # 无商户名无法判定重复,跳过分组(仍计入 checked)
            continue
        groups[(merchant, _normalize_amount(split.get("amount")))].append(split)

    hit = 0
    for (merchant, amount), grouped in groups.items():
        if len(grouped) < 2:
            continue
        hit += 1
        if not send_telegram_message(_build_alert_text(grouped)):
            logger.warning("sentinel_alert_send_failed", merchant=merchant, amount=amount)
    logger.info("sentinel_scan_done", checked=len(splits), groups=hit)
    return {"groups": hit, "checked": len(splits)}
