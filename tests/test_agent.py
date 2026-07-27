from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.agent import runner
from app.agent.tools import TOOL_NAMES, AgentToolError, execute_tool
from app.api import routes_agent
from app.llm.client import LLMClient, LLMError
from app.models.audit import AuditLog
from app.schemas.agent import (
    AgentDecision,
    AgentFinal,
    AgentQuery,
    AgentResponse,
    AgentStep,
)


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


class FakeAgentLLM:
    def __init__(self, decisions, final=None):
        self.decisions = list(decisions)
        self.final = final
        self.decision_calls = []
        self.finish_calls = []

    def decide_agent_action(self, question, today, steps):
        self.decision_calls.append((question, today, list(steps)))
        decision = self.decisions.pop(0)
        if isinstance(decision, Exception):
            raise decision
        return decision

    def finish_agent_answer(self, question, today, steps):
        self.finish_calls.append((question, today, list(steps)))
        return self.final


def test_runner_executes_observation_loop_and_finishes(db_session, monkeypatch):
    llm = FakeAgentLLM(
        [
            AgentDecision(
                action="summarize_spending",
                arguments={
                    "start": "2026-07-01",
                    "end": "2026-07-27",
                    "group_by": "category",
                },
                reasoning_summary="先看本月",
            ),
            AgentDecision(
                action="search_transactions",
                arguments={
                    "start": "2026-07-01",
                    "end": "2026-07-27",
                    "category": "餐饮",
                },
                reasoning_summary="餐饮增长最大",
            ),
            AgentDecision(
                action="finish",
                reasoning_summary="证据足够",
                final_answer="主要增长来自餐饮。",
            ),
        ]
    )
    calls = []
    monkeypatch.setattr(runner, "get_llm_client", lambda: llm)
    monkeypatch.setattr(
        runner,
        "execute_tool",
        lambda name, arguments, today=None: calls.append((name, arguments))
        or {"count": 1, "total": "700.00"},
    )

    result = runner.run_agent(
        "为什么本月增加？", db_session, today=date(2026, 7, 27)
    )

    assert result.answer == "主要增长来自餐饮。"
    assert result.stopped_reason == "finished"
    assert [call[0] for call in calls] == ["summarize_spending", "search_transactions"]
    assert llm.decision_calls[1][2][0].observation["total"] == "700.00"
    events = list(db_session.scalars(select(AuditLog.event).order_by(AuditLog.id)))
    assert events == [
        "agent.started",
        "agent.tool_called",
        "agent.tool_succeeded",
        "agent.tool_called",
        "agent.tool_succeeded",
        "agent.finished",
    ]


def test_runner_forces_finish_after_three_tool_attempts(db_session, monkeypatch):
    llm = FakeAgentLLM(
        [
            AgentDecision(action="bad_tool", reasoning_summary="错误工具"),
            AgentDecision(
                action="search_transactions",
                arguments={"start": "bad"},
                reasoning_summary="参数错误",
            ),
            AgentDecision(
                action="detect_subscriptions",
                arguments={"as_of": "2026-07-27"},
                reasoning_summary="检查订阅",
            ),
        ],
        AgentFinal(answer="仅能确认存在一个订阅。", evidence_summary=["订阅 1 个"]),
    )
    monkeypatch.setattr(runner, "get_llm_client", lambda: llm)

    def fake_execute(name, arguments, today=None):
        if name == "bad_tool":
            raise AgentToolError("未注册")
        if name == "search_transactions":
            AgentQuery(question="").model_dump()
        return {"count": 1}

    monkeypatch.setattr(runner, "execute_tool", fake_execute)

    result = runner.run_agent("调查支出", db_session, today=date(2026, 7, 27))

    assert result.stopped_reason == "limit"
    assert [step.status for step in result.steps] == [
        "tool_error",
        "validation_error",
        "success",
    ]
    assert llm.finish_calls


def test_runner_wraps_and_audits_llm_failure(db_session, monkeypatch):
    llm = FakeAgentLLM([LLMError("网络失败")])
    monkeypatch.setattr(runner, "get_llm_client", lambda: llm)

    with pytest.raises(runner.AgentError, match="AI Agent 服务暂时不可用"):
        runner.run_agent("调查支出", db_session, today=date(2026, 7, 27))

    events = list(db_session.scalars(select(AuditLog.event).order_by(AuditLog.id)))
    assert events == ["agent.started", "agent.failed"]


def test_agent_endpoint_returns_trace_and_steps(client, monkeypatch):
    expected = AgentResponse(
        trace_id="trace-agent-1",
        answer="餐饮增长最多。",
        stopped_reason="finished",
        steps=[],
    )
    monkeypatch.setattr(routes_agent, "run_agent", lambda question, session: expected)

    response = client.post("/api/agent/query", json={"question": "为什么增加？"})

    assert response.status_code == 200
    assert response.json()["trace_id"] == "trace-agent-1"


def test_agent_endpoint_rejects_bad_token(client, monkeypatch):
    monkeypatch.setattr(
        routes_agent,
        "get_settings",
        lambda: SimpleNamespace(console_token="secret"),
    )

    response = client.post("/api/agent/query", json={"question": "查账"})

    assert response.status_code == 401


def test_agent_endpoint_maps_agent_error_to_503(client, monkeypatch):
    monkeypatch.setattr(
        routes_agent,
        "run_agent",
        lambda question, session: (_ for _ in ()).throw(runner.AgentError("boom")),
    )

    response = client.post("/api/agent/query", json={"question": "查账"})

    assert response.status_code == 503
    assert response.json()["detail"] == "AI Agent 服务暂时不可用"


def test_agent_page_renders_visual_client(client):
    response = client.get("/agent")

    assert response.status_code == 200
    assert "财务调查 Agent" in response.text
    assert 'id="agent-form"' in response.text
    assert 'id="messages"' in response.text
    assert 'id="steps"' in response.text
    assert "fetch('/api/agent/query'" in response.text
    assert ".textContent" in response.text
    assert ".innerHTML" not in response.text


def test_agent_page_bootstraps_httponly_cookie(client, monkeypatch):
    monkeypatch.setattr(
        routes_agent,
        "get_settings",
        lambda: SimpleNamespace(console_token="secret"),
    )

    unauthorized = client.get("/agent")
    assert unauthorized.status_code == 401

    response = client.get("/agent?token=secret", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/agent"
    assert "HttpOnly" in response.headers["set-cookie"]

    page = client.get("/agent")
    assert page.status_code == 200


def test_agent_endpoint_accepts_console_cookie(client, monkeypatch):
    expected = AgentResponse(
        trace_id="trace-agent-cookie",
        answer="未发现重复扣费。",
        stopped_reason="finished",
        steps=[],
    )
    monkeypatch.setattr(
        routes_agent,
        "get_settings",
        lambda: SimpleNamespace(console_token="secret"),
    )
    monkeypatch.setattr(routes_agent, "run_agent", lambda question, session: expected)
    client.cookies.set("console_token", "secret")

    response = client.post("/api/agent/query", json={"question": "检查重复扣费"})

    assert response.status_code == 200
    assert response.json()["trace_id"] == "trace-agent-cookie"
