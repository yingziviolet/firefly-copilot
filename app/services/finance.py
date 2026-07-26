from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.schemas.finance import FinanceQuery


def _date(value: Any) -> date | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _amount(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def format_money(value: Any) -> str:
    amount = _amount(value)
    return f"{amount:.2f}" if amount is not None else "?"


def _merchant(split: dict[str, Any], transaction_type: str = "withdrawal") -> str:
    field = "source_name" if transaction_type == "deposit" else "destination_name"
    return str(split.get(field) or "").strip()


def aggregate_transactions(
    splits: list[dict[str, Any]], query: FinanceQuery
) -> Decimal | int:
    matched: list[dict[str, Any]] = []
    for split in splits:
        occurred = _date(split.get("date"))
        if occurred is None or not query.start <= occurred <= query.end:
            continue
        if query.category and str(split.get("category_name") or "") != query.category:
            continue
        merchant = _merchant(split, query.transaction_type)
        if query.merchant and query.merchant.casefold() not in merchant.casefold():
            continue
        matched.append(split)

    if query.metric == "count":
        return len(matched)
    return sum(
        (amount for split in matched if (amount := _amount(split.get("amount"))) is not None),
        Decimal(),
    )


def detect_subscriptions(
    splits: list[dict[str, Any]], as_of: date
) -> list[dict[str, Any]]:
    grouped: dict[str, list[tuple[date, Decimal, str]]] = defaultdict(list)
    for split in splits:
        merchant = _merchant(split)
        occurred = _date(split.get("date"))
        amount = _amount(split.get("amount"))
        if merchant and occurred is not None and amount is not None:
            grouped[merchant.casefold()].append((occurred, amount, merchant))

    subscriptions: list[dict[str, Any]] = []
    for charges in grouped.values():
        charges.sort()
        recent = charges[-3:]
        if len(recent) < 3 or (as_of - recent[-1][0]).days > 40:
            continue
        intervals = zip(recent, recent[1:], strict=False)
        if not all(25 <= (right[0] - left[0]).days <= 35 for left, right in intervals):
            continue
        subscriptions.append(
            {
                "merchant": recent[-1][2],
                "latest_amount": recent[-1][1],
                "previous_amount": recent[-2][1],
                "price_increased": recent[-1][1] > recent[-2][1],
            }
        )
    return sorted(subscriptions, key=lambda item: item["merchant"].casefold())
