from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

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
