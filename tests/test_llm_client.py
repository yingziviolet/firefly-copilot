"""LLMClient 单元测试:注入假 anthropic 客户端,禁真实网络。"""

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app.config import get_settings
from app.llm.client import LLMClient, LLMError, get_llm_client
from app.schemas.classify import DEFAULT_CATEGORIES, LLMClassification
from app.schemas.finance import RawFinanceIntent
from app.schemas.transaction import CanonicalTransaction, TxnDirection, TxnSource


def _make_txn(**overrides) -> CanonicalTransaction:
    data: dict = {
        "source": TxnSource.ALIPAY,
        "direction": TxnDirection.EXPENSE,
        "occurred_at": datetime(2026, 7, 1, 12, 0),
        "amount": Decimal("23.50"),
        "counterparty": "肯德基",
        "description": "午餐",
        "category_hint": "餐饮美食",
    }
    data.update(overrides)
    return CanonicalTransaction(**data)


class _FakeMessages:
    """假 messages 资源:按顺序弹出预设结果;Exception 项直接抛出。"""

    def __init__(self, outcomes: list) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []

    def parse(self, **kwargs) -> SimpleNamespace:
        self.calls.append(kwargs)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(parsed_output=outcome)


class _FakeAnthropic:
    def __init__(self, outcomes: list) -> None:
        self.messages = _FakeMessages(outcomes)


def test_classify_success() -> None:
    expected = LLMClassification(category="餐饮", confidence=0.95, rationale="快餐消费")
    fake = _FakeAnthropic([expected])

    result = LLMClient(client=fake).classify_transaction(_make_txn(), DEFAULT_CATEGORIES)

    assert result is expected
    assert len(fake.messages.calls) == 1

    settings = get_settings()
    call = fake.messages.calls[0]
    assert call["model"] == settings.llm_model
    assert call["max_tokens"] == settings.llm_max_tokens
    assert call["output_config"] == {"effort": settings.llm_effort}
    assert call["output_format"] is LLMClassification

    # user 消息里包含交易摘要:商户/描述/金额/方向/渠道分类提示
    assert call["messages"][0]["role"] == "user"
    content = call["messages"][0]["content"]
    assert "肯德基" in content
    assert "午餐" in content
    assert "23.50" in content
    assert "支出" in content
    assert "餐饮美食" in content


def test_category_out_of_range_retry_success() -> None:
    bad = LLMClassification(category="美食", confidence=0.8, rationale="不在列表")
    good = LLMClassification(category="餐饮", confidence=0.9, rationale="重试正确")
    fake = _FakeAnthropic([bad, good])

    result = LLMClient(client=fake).classify_transaction(_make_txn(), DEFAULT_CATEGORIES)

    assert result.category == "餐饮"
    assert len(fake.messages.calls) == 2
    # 重试的 user 消息里附带纠正提示,并引用了越界分类
    retry_content = fake.messages.calls[1]["messages"][0]["content"]
    assert "美食" in retry_content
    assert "不在候选分类列表中" in retry_content


def test_category_out_of_range_retry_still_fails() -> None:
    bad1 = LLMClassification(category="美食", confidence=0.8, rationale="x")
    bad2 = LLMClassification(category="吃喝", confidence=0.7, rationale="y")
    fake = _FakeAnthropic([bad1, bad2])

    with pytest.raises(LLMError):
        LLMClient(client=fake).classify_transaction(_make_txn(), DEFAULT_CATEGORIES)
    assert len(fake.messages.calls) == 2


def test_sdk_exception_wrapped_as_llm_error() -> None:
    sdk_error = anthropic.APIConnectionError(
        request=httpx.Request("POST", "http://llm.test/v1/messages")
    )
    fake = _FakeAnthropic([sdk_error])

    with pytest.raises(LLMError) as exc_info:
        LLMClient(client=fake).classify_transaction(_make_txn(), DEFAULT_CATEGORIES)
    assert exc_info.value.__cause__ is sdk_error


def test_none_parsed_output_raises_llm_error() -> None:
    fake = _FakeAnthropic([None])

    with pytest.raises(LLMError):
        LLMClient(client=fake).classify_transaction(_make_txn(), DEFAULT_CATEGORIES)


def test_system_prompt_contains_all_categories() -> None:
    expected = LLMClassification(category="餐饮", confidence=0.95, rationale="z")
    fake = _FakeAnthropic([expected])

    LLMClient(client=fake).classify_transaction(_make_txn(), DEFAULT_CATEGORIES)

    system = fake.messages.calls[0]["system"]
    for category in DEFAULT_CATEGORIES:
        assert category in system
    assert "只能从列表中选" in system


def test_parse_finance_query_uses_loose_intent_schema() -> None:
    raw = RawFinanceIntent(
        start="2026-06-01",
        end="2026-06-30",
        transaction_type="支出",
        category="餐饮",
        metric="金额",
    )
    fake = _FakeAnthropic([raw])

    result = LLMClient(client=fake).parse_finance_query(
        "六月餐饮花了多少", today=date(2026, 7, 27)
    )

    assert result.start == date(2026, 6, 1)
    assert result.end == date(2026, 6, 30)
    assert result.transaction_type == "withdrawal"
    assert result.metric == "sum"
    call = fake.messages.calls[0]
    assert call["output_format"] is RawFinanceIntent
    assert "2026-07-27" in call["system"]
    assert "餐饮" in call["system"]
    assert "不得生成 SQL" in call["system"]
    assert "只返回 JSON" in call["system"]
    assert "不要推断分类" in call["system"]
    assert call["temperature"] == 0
    assert call["thinking"] == {"type": "disabled"}
    assert "output_config" not in call
    assert call["messages"][0]["content"] == "六月餐饮花了多少"


def test_parse_finance_query_retries_invalid_intent() -> None:
    invalid = RawFinanceIntent(
        start="九十天前",
        end="今天",
        transaction_type="支出",
        category="交通",
    )
    corrected = RawFinanceIntent(
        start="2026-04-28",
        end="2026-07-27",
        transaction_type="withdrawal",
        category="交通",
        metric="sum",
    )
    fake = _FakeAnthropic([invalid, corrected])

    result = LLMClient(client=fake).parse_finance_query(
        "过去一段时间交通花了多少", today=date(2026, 7, 27)
    )

    assert result.start == date(2026, 4, 28)
    assert result.end == date(2026, 7, 27)
    assert len(fake.messages.calls) == 2
    assert "上次参数无效" in fake.messages.calls[1]["messages"][0]["content"]


def test_get_llm_client_is_cached_singleton() -> None:
    get_llm_client.cache_clear()
    try:
        a = get_llm_client()
        b = get_llm_client()
        assert a is b
        assert isinstance(a, LLMClient)
    finally:
        get_llm_client.cache_clear()
