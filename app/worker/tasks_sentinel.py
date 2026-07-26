"""哨兵任务(Celery beat 定时):重复扣费检测。

scan_duplicate_charges(days=3):
1. firefly.list_transactions(近 days 天, withdrawal)
2. 按 (destination_name 归一化, amount) 分组,组内 >= 2 笔视为疑似重复扣费
3. 对每组生成告警文本(商户/金额/日期列表),notifier.notify 推送
4. 返回 {"groups": 命中组数, "checked": 扫描笔数}
Firefly 不可达时记日志返回 {"error": ...},不抛异常(避免 beat 无限重试刷屏)。
"""

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.logger import get_logger
from app.services.finance import detect_subscriptions, format_money
from app.services.firefly_client import FireflyError, get_firefly_client
from app.services.notifier import notify
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
    amount = format_money(first.get("amount"))
    lines = [
        "疑似重复扣费提醒",
        f"商户:{merchant}",
        f"金额:{amount}",
        f"共 {len(splits)} 笔:",
    ]
    lines.extend(f"- {s.get('date', '未知日期')}" for s in splits)
    return "\n".join(lines)


def _split_date(split: dict[str, Any]) -> date | None:
    try:
        return datetime.fromisoformat(str(split.get("date")).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _duplicate_groups(splits: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for split in splits:
        merchant = _normalize_merchant(split.get("destination_name"))
        if merchant:
            groups[(merchant, _normalize_amount(split.get("amount")))].append(split)
    return [grouped for grouped in groups.values() if len(grouped) >= 2]


def _sum_amounts(splits: list[dict[str, Any]]) -> Decimal:
    total = Decimal()
    for split in splits:
        try:
            total += Decimal(str(split.get("amount")))
        except (InvalidOperation, ValueError):
            continue
    return total


def _build_weekly_text(
    start: date,
    end: date,
    withdrawals: list[dict[str, Any]],
    deposits: list[dict[str, Any]],
    subscriptions: list[dict[str, Any]],
    duplicates: list[list[dict[str, Any]]],
) -> str:
    expense = _sum_amounts(withdrawals)
    income = _sum_amounts(deposits)
    categories: dict[str, Decimal] = defaultdict(Decimal)
    for split in withdrawals:
        category = str(split.get("category_name") or "未分类")
        try:
            categories[category] += Decimal(str(split.get("amount")))
        except (InvalidOperation, ValueError):
            continue

    lines = [
        "每周财务简报",
        f"{start.isoformat()} 至 {end.isoformat()}",
        "",
        "收支概览",
        f"总收入:{format_money(income)}",
        f"总支出:{format_money(expense)}",
        f"净额:{format_money(income - expense)}",
        f"交易:收入 {len(deposits)} 笔 / 支出 {len(withdrawals)} 笔",
        "",
        "支出分类",
    ]
    lines.extend(
        f"- {category}:{format_money(amount)}"
        for category, amount in sorted(categories.items(), key=lambda item: item[1], reverse=True)
    )
    if not categories:
        lines.append("- 无支出")

    lines.extend(["", "订阅管家"])
    if subscriptions:
        for item in subscriptions:
            detail = f"- {item['merchant']}:{format_money(item['latest_amount'])}"
            if item["price_increased"]:
                detail += (
                    f"（涨价 {format_money(item['previous_amount'])}"
                    f" → {format_money(item['latest_amount'])}）"
                )
            lines.append(detail)
    else:
        lines.append("- 本周未发现订阅涨价或持续扣费")

    lines.extend(["", "重复扣费"])
    if duplicates:
        for grouped in duplicates:
            first = grouped[0]
            lines.append(
                f"- {str(first.get('destination_name') or '未知商户').strip()} "
                f"{format_money(first.get('amount'))}，共 {len(grouped)} 笔"
            )
    else:
        lines.append("- 本周未发现疑似重复扣费")
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

    groups = _duplicate_groups(splits)
    if groups and not notify("\n\n".join(_build_alert_text(grouped) for grouped in groups)):
        logger.warning("sentinel_alert_send_failed", groups=len(groups))
    hit = len(groups)
    logger.info("sentinel_scan_done", checked=len(splits), groups=hit)
    return {"groups": hit, "checked": len(splits)}


@celery_app.task(name="app.worker.tasks_sentinel.send_weekly_digest")
def send_weekly_digest(today_iso: str | None = None) -> dict[str, Any]:
    today = date.fromisoformat(today_iso) if today_iso else date.today()
    end = today - timedelta(days=today.weekday() + 1)
    start = end - timedelta(days=6)
    history_start = end - timedelta(days=119)
    try:
        client = get_firefly_client()
        history = client.list_transactions(history_start, end, txn_type="withdrawal")
        deposits = client.list_transactions(start, end, txn_type="deposit")
    except (FireflyError, httpx.HTTPError) as exc:
        logger.warning("weekly_digest_firefly_unreachable", error=str(exc))
        return {"error": str(exc)}

    withdrawals = [
        split
        for split in history
        if (occurred := _split_date(split)) is not None and start <= occurred <= end
    ]
    subscriptions = detect_subscriptions(history, as_of=end)
    duplicates = _duplicate_groups(withdrawals)
    text = _build_weekly_text(
        start, end, withdrawals, deposits, subscriptions, duplicates
    )
    if not notify(text):
        logger.warning("weekly_digest_send_failed")
    logger.info(
        "weekly_digest_done",
        withdrawals=len(withdrawals),
        deposits=len(deposits),
        subscriptions=len(subscriptions),
        duplicates=len(duplicates),
    )
    return {
        "withdrawals": len(withdrawals),
        "deposits": len(deposits),
        "subscriptions": len(subscriptions),
        "duplicates": len(duplicates),
    }
