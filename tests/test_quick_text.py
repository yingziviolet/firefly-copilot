"""快捷记账文本解析单测。"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.parsers.quick_text import parse_quick_expense
from app.schemas.transaction import TxnDirection, TxnSource


def test_basic_expense():
    txn = parse_quick_expense("早餐 15")
    assert txn is not None
    assert txn.amount == Decimal("15")
    assert txn.counterparty == "早餐"
    assert txn.source == TxnSource.MANUAL
    assert txn.direction == TxnDirection.EXPENSE


def test_decimal_amount():
    txn = parse_quick_expense("打车 23.5")
    assert txn is not None
    assert txn.amount == Decimal("23.5")
    assert txn.counterparty == "打车"


def test_currency_suffix_stripped():
    txn = parse_quick_expense("奶茶15元")
    assert txn is not None
    assert txn.amount == Decimal("15")
    assert txn.counterparty == "奶茶"


def test_yesterday_date_word():
    txn = parse_quick_expense("昨天 午饭 20")
    assert txn is not None
    assert txn.amount == Decimal("20")
    assert txn.counterparty == "午饭"
    expected = datetime.now(UTC) - timedelta(days=1)
    assert abs((txn.occurred_at - expected).total_seconds()) < 60


def test_day_before_yesterday():
    txn = parse_quick_expense("前天 电影 45")
    assert txn is not None
    expected = datetime.now(UTC) - timedelta(days=2)
    assert abs((txn.occurred_at - expected).total_seconds()) < 60


def test_last_number_wins():
    txn = parse_quick_expense("公交2号线 3")
    assert txn is not None
    assert txn.amount == Decimal("3")


def test_no_amount_returns_none():
    assert parse_quick_expense("没有数字") is None


def test_zero_amount_returns_none():
    assert parse_quick_expense("白嫖 0") is None


def test_empty_returns_none():
    assert parse_quick_expense("") is None
    assert parse_quick_expense("   ") is None


def test_amount_only_uses_placeholder_counterparty():
    txn = parse_quick_expense("15")
    assert txn is not None
    assert txn.counterparty == "未知"
