from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.finance import FinanceQuery, RawFinanceIntent
from app.services.finance import aggregate_transactions, detect_subscriptions, format_money


def _split(
    merchant: str,
    amount: str,
    occurred: str,
    category: str = "订阅服务",
) -> dict:
    return {
        "destination_name": merchant,
        "amount": amount,
        "date": occurred,
        "category_name": category,
    }


def test_finance_query_rejects_invalid_date_ranges():
    with pytest.raises(ValidationError):
        FinanceQuery(start=date(2026, 7, 2), end=date(2026, 7, 1))
    with pytest.raises(ValidationError):
        FinanceQuery(start=date(2025, 1, 1), end=date(2026, 7, 1))


def test_finance_query_accepts_common_llm_field_aliases():
    query = FinanceQuery.model_validate(
        {
            "start_date": "2026-06-01",
            "end_date": "2026-06-30",
            "type": "expense",
            "merchant": "美团",
            "metric": "sum",
        }
    )

    assert query.start == date(2026, 6, 1)
    assert query.end == date(2026, 6, 30)
    assert query.transaction_type == "withdrawal"


def test_raw_finance_intent_normalizes_chinese_values():
    raw = RawFinanceIntent(
        start="2026-06-12",
        end="2026-07-27",
        transaction_type="支出",
        category="交通消费",
        metric="金额",
    )

    query = raw.to_query("1个半月内交通花了多少钱", date(2026, 7, 27))

    assert query == FinanceQuery(
        start=date(2026, 6, 12),
        end=date(2026, 7, 27),
        transaction_type="withdrawal",
        category="交通",
        metric="sum",
    )


def test_raw_finance_intent_corrects_calendar_period_and_count():
    raw = RawFinanceIntent(
        transaction_type="消费",
        category="餐饮支出",
        metric="笔数",
    )

    query = raw.to_query("这两个月餐饮有多少笔", date(2026, 7, 27))

    assert query.start == date(2026, 6, 1)
    assert query.end == date(2026, 7, 27)
    assert query.transaction_type == "withdrawal"
    assert query.category == "餐饮"
    assert query.metric == "count"


def test_raw_finance_intent_corrects_relative_day_period():
    raw = RawFinanceIntent(
        start="15天前",
        end="今天",
        transaction_type="支出",
        category="交通",
    )

    query = raw.to_query("15天内交通花了多少钱", date(2026, 7, 27))

    assert query.start == date(2026, 7, 12)
    assert query.end == date(2026, 7, 27)


def test_raw_finance_intent_normalizes_common_category_word():
    query = RawFinanceIntent(metric="count").to_query(
        "今年打车多少笔", date(2026, 7, 27)
    )

    assert query.category == "交通"
    assert query.metric == "count"


def test_format_money_always_uses_two_decimal_places():
    assert format_money(Decimal("150.000000000000")) == "150.00"
    assert format_money(Decimal("335.050000000000")) == "335.05"
    assert format_money(Decimal("0.020000000000")) == "0.02"


def test_aggregate_filters_and_sums_with_decimal():
    query = FinanceQuery(
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
        category="餐饮",
        merchant="美团",
    )
    splits = [
        _split("美团外卖", "12.10", "2026-07-02T12:00:00+08:00", "餐饮"),
        _split("美团外卖", "7.20", "2026-07-03T12:00:00+08:00", "餐饮"),
        _split("饿了么", "30", "2026-07-04T12:00:00+08:00", "餐饮"),
        _split("美团外卖", "99", "2026-06-30T12:00:00+08:00", "餐饮"),
    ]

    assert aggregate_transactions(splits, query) == Decimal("19.30")
    assert aggregate_transactions(splits, query.model_copy(update={"metric": "count"})) == 2


def test_detects_active_monthly_subscription_and_price_increase():
    splits = [
        _split("视频会员", "25", "2026-05-01"),
        _split(" 视频会员 ", "25", "2026-05-31"),
        _split("视频会员", "30", "2026-06-30"),
    ]

    assert detect_subscriptions(splits, as_of=date(2026, 7, 6)) == [
        {
            "merchant": "视频会员",
            "latest_amount": Decimal("30"),
            "previous_amount": Decimal("25"),
            "price_increased": True,
        }
    ]


def test_irregular_or_stale_charges_are_not_subscriptions():
    splits = [
        _split("网购平台", "10", "2026-01-01"),
        _split("网购平台", "20", "2026-01-05"),
        _split("网购平台", "30", "2026-03-20"),
        _split("旧会员", "10", "2026-01-01"),
        _split("旧会员", "10", "2026-01-31"),
        _split("旧会员", "10", "2026-03-02"),
    ]

    assert detect_subscriptions(splits, as_of=date(2026, 7, 6)) == []
