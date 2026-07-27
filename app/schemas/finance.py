import re
from calendar import monthrange
from datetime import date, timedelta
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator

_DIRECTION_ALIASES = {
    "withdrawal": "withdrawal",
    "expense": "withdrawal",
    "支出": "withdrawal",
    "消费": "withdrawal",
    "deposit": "deposit",
    "income": "deposit",
    "收入": "deposit",
    "入账": "deposit",
}
_METRIC_ALIASES = {
    "sum": "sum",
    "金额": "sum",
    "合计": "sum",
    "总额": "sum",
    "count": "count",
    "笔数": "count",
    "次数": "count",
}
_CHINESE_MONTHS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
}


def _shift_months(value: date, months: int) -> date:
    index = value.year * 12 + value.month - 1 + months
    year, zero_based_month = divmod(index, 12)
    month = zero_based_month + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def _parse_date(value: str | None, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def _query_period(
    question: str,
    today: date,
    raw_start: str | None,
    raw_end: str | None,
) -> tuple[date, date]:
    month_start = today.replace(day=1)
    previous_month_end = month_start - timedelta(days=1)

    if "这两个月" in question:
        return previous_month_end.replace(day=1), today
    if "最近两个月" in question:
        return _shift_months(today, -2), today
    if re.search(r"(?:1|一)(?:个)?半月", question):
        return today - timedelta(days=45), today
    if "本月" in question or "这个月" in question:
        return month_start, today
    if "上月" in question or "上个月" in question:
        return previous_month_end.replace(day=1), previous_month_end
    if "今年" in question:
        return today.replace(month=1, day=1), today

    explicit_month = re.search(r"(十一|十二|十|[一二三四五六七八九]|\d{1,2})月", question)
    if explicit_month:
        token = explicit_month.group(1)
        month = _CHINESE_MONTHS.get(token, int(token) if token.isdigit() else 0)
        if not 1 <= month <= 12:
            raise ValueError("月份必须在 1 到 12 之间")
        return (
            date(today.year, month, 1),
            date(today.year, month, monthrange(today.year, month)[1]),
        )

    has_time_hint = bool(re.search(r"月|年|天|日|周|最近|过去|以来|内|\d{4}[-/]", question))
    if not has_time_hint:
        return month_start, today
    return _parse_date(raw_start, "start"), _parse_date(raw_end, "end")


def _normalize_direction(value: str | None, question: str) -> str:
    normalized = _DIRECTION_ALIASES.get((value or "").strip().casefold())
    if normalized:
        return normalized
    if any(word in question for word in ("收入", "到账", "赚了", "工资")):
        return "deposit"
    return "withdrawal"


def _normalize_metric(value: str | None, question: str) -> str:
    normalized = _METRIC_ALIASES.get((value or "").strip().casefold())
    if normalized:
        return normalized
    return "count" if re.search(r"几笔|多少笔|笔数|次数", question) else "sum"


def _normalize_category(value: str | None) -> str | None:
    category = (value or "").strip()
    for suffix in ("支出", "消费", "花费", "费用"):
        if category.endswith(suffix):
            category = category[: -len(suffix)].strip()
            break
    return category or None


class FinanceQuery(BaseModel):
    start: date = Field(validation_alias=AliasChoices("start", "start_date"))
    end: date = Field(validation_alias=AliasChoices("end", "end_date"))
    transaction_type: Literal["withdrawal", "deposit"] = Field(
        default="withdrawal",
        validation_alias=AliasChoices("transaction_type", "type"),
    )
    category: str | None = None
    merchant: str | None = None
    metric: Literal["sum", "count"] = "sum"

    @field_validator("transaction_type", mode="before")
    @classmethod
    def normalize_transaction_type(cls, value: str) -> str:
        return _DIRECTION_ALIASES.get(str(value).strip().casefold(), value)

    @field_validator("metric", mode="before")
    @classmethod
    def normalize_metric(cls, value: str) -> str:
        return _METRIC_ALIASES.get(str(value).strip().casefold(), value)

    @model_validator(mode="after")
    def validate_range(self) -> "FinanceQuery":
        days = (self.end - self.start).days
        if days < 0:
            raise ValueError("结束日期不能早于开始日期")
        if days > 366:
            raise ValueError("查询范围不能超过 366 天")
        return self


class RawFinanceIntent(BaseModel):
    start: str | None = Field(default=None, validation_alias=AliasChoices("start", "start_date"))
    end: str | None = Field(default=None, validation_alias=AliasChoices("end", "end_date"))
    transaction_type: str | None = Field(
        default=None,
        validation_alias=AliasChoices("transaction_type", "type", "direction"),
    )
    category: str | None = None
    merchant: str | None = None
    metric: str | None = None

    def to_query(self, question: str, today: date) -> FinanceQuery:
        start, end = _query_period(question, today, self.start, self.end)
        return FinanceQuery(
            start=start,
            end=end,
            transaction_type=_normalize_direction(self.transaction_type, question),
            category=_normalize_category(self.category),
            merchant=(self.merchant or "").strip() or None,
            metric=_normalize_metric(self.metric, question),
        )
