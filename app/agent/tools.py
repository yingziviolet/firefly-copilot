from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, model_validator

from app.services.finance import detect_subscriptions, find_duplicate_groups
from app.services.firefly_client import FireflyClient, get_firefly_client


class AgentToolError(RuntimeError):
    pass


class DateRangeInput(BaseModel):
    start: date = Field(validation_alias=AliasChoices("start", "start_date"))
    end: date = Field(validation_alias=AliasChoices("end", "end_date"))

    @model_validator(mode="after")
    def validate_range(self) -> "DateRangeInput":
        days = (self.end - self.start).days
        if days < 0:
            raise ValueError("结束日期不能早于开始日期")
        if days > 366:
            raise ValueError("查询范围不能超过 366 天")
        return self


class SpendingSummaryInput(DateRangeInput):
    group_by: Literal["category", "merchant"] = "category"


class TransactionSearchInput(DateRangeInput):
    category: str | None = None
    merchant: str | None = None
    limit: int = Field(default=10, ge=1, le=20)


class SubscriptionInput(BaseModel):
    as_of: date


class DuplicateInput(BaseModel):
    days: int = Field(default=7, ge=1, le=31)


TOOL_NAMES = {
    "summarize_spending",
    "search_transactions",
    "detect_subscriptions",
    "find_duplicate_charges",
}


def _amount(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _merchant(split: dict[str, Any]) -> str:
    return str(split.get("destination_name") or "").strip()


def _summary(data: SpendingSummaryInput, firefly: FireflyClient) -> dict[str, Any]:
    splits = firefly.list_transactions(data.start, data.end, txn_type="withdrawal")
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"amount": Decimal(), "count": 0}
    )
    total = Decimal()
    count = 0
    for split in splits:
        amount = _amount(split.get("amount"))
        if amount is None:
            continue
        name = (
            str(split.get("category_name") or "未分类")
            if data.group_by == "category"
            else (_merchant(split) or "未知商户")
        )
        total += amount
        count += 1
        grouped[name]["amount"] += amount
        grouped[name]["count"] += 1
    groups = sorted(grouped.items(), key=lambda item: item[1]["amount"], reverse=True)[:20]
    return {
        "total": f"{total:.2f}",
        "count": count,
        "groups": [
            {"name": name, "amount": f"{stats['amount']:.2f}", "count": stats["count"]}
            for name, stats in groups
        ],
    }


def _search(data: TransactionSearchInput, firefly: FireflyClient) -> dict[str, Any]:
    splits = firefly.list_transactions(data.start, data.end, txn_type="withdrawal")
    matched = []
    for split in splits:
        merchant = _merchant(split)
        if data.category and str(split.get("category_name") or "") != data.category:
            continue
        if data.merchant and data.merchant.casefold() not in merchant.casefold():
            continue
        amount = _amount(split.get("amount"))
        if amount is not None:
            matched.append((amount, split, merchant))
    matched.sort(key=lambda item: item[0], reverse=True)
    transactions = [
        {
            "date": str(split.get("date") or "")[:10],
            "merchant": merchant,
            "category": str(split.get("category_name") or "未分类"),
            "amount": f"{amount:.2f}",
            "description": str(split.get("description") or "")[:120],
        }
        for amount, split, merchant in matched[: data.limit]
    ]
    return {"count": len(transactions), "transactions": transactions}


def _subscriptions(data: SubscriptionInput, firefly: FireflyClient) -> dict[str, Any]:
    start = data.as_of - timedelta(days=119)
    splits = firefly.list_transactions(start, data.as_of, txn_type="withdrawal")
    items = detect_subscriptions(splits, data.as_of)
    return {
        "count": len(items),
        "subscriptions": [
            {
                **item,
                "latest_amount": f"{item['latest_amount']:.2f}",
                "previous_amount": f"{item['previous_amount']:.2f}",
            }
            for item in items
        ],
    }


def _duplicates(data: DuplicateInput, firefly: FireflyClient, today: date) -> dict[str, Any]:
    splits = firefly.list_transactions(
        today - timedelta(days=data.days), today, txn_type="withdrawal"
    )
    groups = find_duplicate_groups(splits)
    return {
        "count": len(groups),
        "groups": [
            {
                "merchant": _merchant(group[0]),
                "amount": f"{_amount(group[0].get('amount')) or Decimal():.2f}",
                "dates": [str(item.get("date") or "")[:10] for item in group],
            }
            for group in groups[:20]
        ],
    }


def execute_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    firefly: FireflyClient | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    client = firefly or get_firefly_client()
    current = today or date.today()
    if name == "summarize_spending":
        return _summary(SpendingSummaryInput.model_validate(arguments), client)
    if name == "search_transactions":
        return _search(TransactionSearchInput.model_validate(arguments), client)
    if name == "detect_subscriptions":
        return _subscriptions(SubscriptionInput.model_validate(arguments), client)
    if name == "find_duplicate_charges":
        return _duplicates(DuplicateInput.model_validate(arguments), client, current)
    raise AgentToolError(f"未注册工具: {name}")
