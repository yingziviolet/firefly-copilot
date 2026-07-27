from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agent.tools import TOOL_NAMES, AgentToolError, execute_tool
from app.llm.client import LLMClient, LLMError
from app.schemas.agent import AgentDecision, AgentFinal, AgentQuery, AgentStep


class FakeMessages:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(parsed_output=outcome)


class FakeAnthropic:
    def __init__(self, outcomes):
        self.messages = FakeMessages(outcomes)


def test_agent_query_rejects_blank_and_long_questions():
    with pytest.raises(ValidationError):
        AgentQuery(question="  ")
    with pytest.raises(ValidationError):
        AgentQuery(question="x" * 501)


def test_agent_decision_requires_answer_only_for_finish():
    with pytest.raises(ValidationError):
        AgentDecision(action="finish", reasoning_summary="完成")
    with pytest.raises(ValidationError):
        AgentDecision(
            action="summarize_spending",
            reasoning_summary="继续查询",
            final_answer="不应出现",
        )


def test_llm_decides_from_question_and_observations():
    expected = AgentDecision(
        action="search_transactions",
        arguments={"start": "2026-07-01", "end": "2026-07-27", "limit": 10},
        reasoning_summary="需要查看大额明细",
    )
    fake = FakeAnthropic([expected])
    step = AgentStep(
        tool="summarize_spending",
        arguments={"start": "2026-07-01", "end": "2026-07-27", "group_by": "category"},
        status="success",
        reasoning_summary="先看分类",
        observation={"total": "2400.00", "count": 42},
    )

    result = LLMClient(client=fake).decide_agent_action(
        "为什么本月支出增加？", date(2026, 7, 27), [step]
    )

    assert result is expected
    call = fake.messages.calls[0]
    assert call["output_format"] is AgentDecision
    assert "2026-07-27" in call["system"]
    assert "summarize_spending" in call["system"]
    assert "2400.00" in call["messages"][0]["content"]
    assert "不得生成 SQL" in call["system"]


def test_llm_forced_finish_uses_agent_final_schema():
    expected = AgentFinal(answer="餐饮是主要增长来源", evidence_summary=["餐饮增加 700 元"])
    fake = FakeAnthropic([expected])

    result = LLMClient(client=fake).finish_agent_answer(
        "为什么增加？", date(2026, 7, 27), []
    )

    assert result is expected
    assert fake.messages.calls[0]["output_format"] is AgentFinal


def test_llm_agent_missing_output_is_wrapped():
    fake = FakeAnthropic([None])
    with pytest.raises(LLMError):
        LLMClient(client=fake).decide_agent_action("查账", date(2026, 7, 27), [])


class FakeFirefly:
    def __init__(self, splits):
        self.splits = splits
        self.calls = []

    def list_transactions(self, start, end, txn_type="withdrawal"):
        self.calls.append((start, end, txn_type))
        return self.splits


SPLITS = [
    {
        "date": "2026-07-10",
        "amount": "100.00",
        "destination_name": "海底捞",
        "category_name": "餐饮",
        "description": "聚餐",
    },
    {
        "date": "2026-07-11",
        "amount": "50.00",
        "destination_name": "星巴克",
        "category_name": "餐饮",
        "description": "咖啡",
    },
    {
        "date": "2026-07-12",
        "amount": "20.00",
        "destination_name": "地铁",
        "category_name": "交通",
        "description": "通勤",
    },
]


def test_tool_registry_contains_only_four_read_tools():
    assert TOOL_NAMES == {
        "summarize_spending",
        "search_transactions",
        "detect_subscriptions",
        "find_duplicate_charges",
    }
    assert all("store" not in name and "write" not in name for name in TOOL_NAMES)


def test_summarize_spending_uses_decimal_and_groups():
    result = execute_tool(
        "summarize_spending",
        {
            "start": "2026-07-01",
            "end": "2026-07-31",
            "group_by": "category",
        },
        firefly=FakeFirefly(SPLITS),
        today=date(2026, 7, 27),
    )

    assert result["total"] == "170.00"
    assert result["count"] == 3
    assert result["groups"][0] == {"name": "餐饮", "amount": "150.00", "count": 2}


def test_search_transactions_filters_and_limits():
    result = execute_tool(
        "search_transactions",
        {
            "start": "2026-07-01",
            "end": "2026-07-31",
            "category": "餐饮",
            "merchant": "星巴克",
            "limit": 1,
        },
        firefly=FakeFirefly(SPLITS),
        today=date(2026, 7, 27),
    )

    assert result["count"] == 1
    assert result["transactions"][0]["merchant"] == "星巴克"
    assert result["transactions"][0]["amount"] == "50.00"


def test_subscription_and_duplicate_tools_return_bounded_results():
    subscriptions = [
        {
            "date": value,
            "amount": amount,
            "destination_name": "视频会员",
        }
        for value, amount in (
            ("2026-05-27", "20"),
            ("2026-06-27", "20"),
            ("2026-07-27", "25"),
        )
    ]
    duplicate = [
        {"date": "2026-07-26", "amount": "88.00", "destination_name": "同一商户"},
        {"date": "2026-07-27", "amount": "88.0", "destination_name": "同一商户"},
    ]

    subscription_result = execute_tool(
        "detect_subscriptions",
        {"as_of": "2026-07-27"},
        firefly=FakeFirefly(subscriptions),
    )
    duplicate_result = execute_tool(
        "find_duplicate_charges",
        {"days": 7},
        firefly=FakeFirefly(duplicate),
        today=date(2026, 7, 27),
    )

    assert subscription_result["subscriptions"][0]["latest_amount"] == "25.00"
    assert duplicate_result["groups"][0]["dates"] == ["2026-07-26", "2026-07-27"]


def test_tool_rejects_invalid_range_and_unknown_name():
    with pytest.raises(ValidationError):
        execute_tool(
            "summarize_spending",
            {
                "start": "2025-01-01",
                "end": "2026-07-31",
                "group_by": "category",
            },
            firefly=FakeFirefly([]),
            today=date(2026, 7, 27),
        )
    with pytest.raises(AgentToolError):
        execute_tool("store_transaction", {}, firefly=FakeFirefly([]))
