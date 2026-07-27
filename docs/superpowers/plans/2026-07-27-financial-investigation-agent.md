# Financial Investigation Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only, audited financial investigation Agent that lets the LLM choose among four typed tools for at most three tool calls through `POST /api/agent/query`.

**Architecture:** Keep the existing Anthropic-compatible SDK and add a typed action-selection loop rather than a framework. `AgentRunner` owns state and limits, `agent/tools.py` owns the four read-only adapters, existing services continue to own Firefly access and deterministic calculations, and the API only translates authentication/errors.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, Anthropic SDK structured output, SQLAlchemy, Firefly III REST API, pytest.

---

## File map

**Create**

- `app/agent/__init__.py` — package marker.
- `app/agent/tools.py` — typed, read-only tool registry and execution.
- `app/agent/runner.py` — model/action/observation loop and audit events.
- `app/schemas/agent.py` — API, model-decision, state, and response contracts.
- `app/api/routes_agent.py` — authenticated FastAPI endpoint.
- `tests/test_agent.py` — all Agent schema, LLM, tool, runner, route, and audit tests.

**Modify**

- `app/llm/client.py` — add structured decision and forced-final-answer calls.
- `app/services/finance.py` — expose duplicate grouping as reusable pure logic.
- `app/worker/tasks_sentinel.py` — consume the shared duplicate grouping helper.
- `app/main.py` — register the Agent router.
- `README.md` — document the implemented Agent and strict boundaries.

**Do not modify**

- Database migrations.
- Existing write/import/review paths.
- Dependencies in `pyproject.toml`.

### Task 1: Agent contracts and structured LLM turns

**Files:**

- Create: `app/schemas/agent.py`
- Modify: `app/llm/client.py`
- Create/Test: `tests/test_agent.py`

- [ ] **Step 1: Write failing schema and LLM tests**

Create `tests/test_agent.py` with the shared fake Anthropic client and these first tests:

```python
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
            arguments={},
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
```

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_agent.py -q
```

Expected: collection fails because `app.schemas.agent` does not exist.

- [ ] **Step 3: Implement the Agent schemas**

Create `app/schemas/agent.py`:

```python
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class AgentQuery(BaseModel):
    question: str = Field(min_length=1, max_length=500)

    @field_validator("question")
    @classmethod
    def strip_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("问题不能为空")
        return value


class AgentDecision(BaseModel):
    action: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)
    reasoning_summary: str = Field(min_length=1, max_length=200)
    final_answer: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_finish(self) -> "AgentDecision":
        if self.action == "finish" and not self.final_answer:
            raise ValueError("finish 必须包含 final_answer")
        if self.action != "finish" and self.final_answer is not None:
            raise ValueError("工具调用不能包含 final_answer")
        return self


class AgentFinal(BaseModel):
    answer: str = Field(min_length=1, max_length=2000)
    evidence_summary: list[str] = Field(default_factory=list, max_length=5)


class AgentStep(BaseModel):
    tool: str
    arguments: dict[str, Any]
    status: Literal["success", "validation_error", "tool_error"]
    reasoning_summary: str
    observation: dict[str, Any]


class AgentStepSummary(BaseModel):
    tool: str
    arguments: dict[str, Any]
    status: Literal["success", "validation_error", "tool_error"]
    observation_summary: str


class AgentResponse(BaseModel):
    trace_id: str
    answer: str
    stopped_reason: Literal["finished", "limit"]
    steps: list[AgentStepSummary]
```

- [ ] **Step 4: Add the two structured Agent calls to `LLMClient`**

Add imports to `app/llm/client.py`:

```python
import json

from app.schemas.agent import AgentDecision, AgentFinal, AgentStep
```

Add these constants after `_DIRECTION_LABELS`:

```python
_AGENT_TOOLS = """
- summarize_spending: 按分类或商户汇总指定日期范围的支出
- search_transactions: 按日期、分类、商户查询有限条交易
- detect_subscriptions: 检查持续订阅和涨价
- find_duplicate_charges: 检查近期相同商户、相同金额的疑似重复扣费
"""
```

Add these methods to `LLMClient` before `_parse_once`:

```python
    def decide_agent_action(
        self, question: str, today: date, steps: list[AgentStep]
    ) -> AgentDecision:
        system = (
            f"你是只读财务调查 Agent。今天是 {today.isoformat()}。\n"
            f"可用工具：\n{_AGENT_TOOLS}\n"
            "根据用户目标和已有 observation 选择一个工具，信息足够时 action=finish。"
            "只能使用 observation 中的事实，不得自行计算金额，不得生成 SQL、URL、代码或投资建议。"
            "不要重复调用参数完全相同的工具。reasoning_summary 只写简短决策依据。"
        )
        content = json.dumps(
            {
                "question": question,
                "steps": [step.model_dump(mode="json") for step in steps],
            },
            ensure_ascii=False,
        )
        return self._parse_agent(system, content, AgentDecision)

    def finish_agent_answer(
        self, question: str, today: date, steps: list[AgentStep]
    ) -> AgentFinal:
        system = (
            f"你是只读财务调查 Agent。今天是 {today.isoformat()}。"
            "工具调用次数已到上限，只能根据已有 observation 给出结论。"
            "不得补造数字；信息不足必须明确说明。"
        )
        content = json.dumps(
            {
                "question": question,
                "steps": [step.model_dump(mode="json") for step in steps],
            },
            ensure_ascii=False,
        )
        return self._parse_agent(system, content, AgentFinal)

    def _parse_agent(self, system: str, content: str, output_format: Any) -> Any:
        try:
            response = self._client.messages.parse(
                model=self._settings.llm_model,
                max_tokens=self._settings.llm_max_tokens,
                temperature=0,
                thinking={"type": "disabled"},
                system=system,
                messages=[{"role": "user", "content": content}],
                output_format=output_format,
            )
        except Exception as exc:
            raise LLMError(f"LLM Agent 调用失败: {exc}") from exc
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise LLMError("LLM Agent 未返回结构化输出")
        return parsed
```

- [ ] **Step 5: Run the focused tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_agent.py -q
```

Expected: `5 passed`.

- [ ] **Step 6: Commit Task 1**

```powershell
git add app/schemas/agent.py app/llm/client.py tests/test_agent.py
git commit -m "feat: add typed agent decisions"
```

### Task 2: Four read-only financial tools

**Files:**

- Create: `app/agent/__init__.py`
- Create: `app/agent/tools.py`
- Modify: `app/services/finance.py`
- Modify: `app/worker/tasks_sentinel.py`
- Test: `tests/test_agent.py`

- [ ] **Step 1: Add failing tool tests**

Append to `tests/test_agent.py`:

```python
from app.agent.tools import (
    TOOL_NAMES,
    AgentToolError,
    SpendingSummaryInput,
    execute_tool,
)


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
```

- [ ] **Step 2: Run the tool tests and confirm failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_agent.py -q
```

Expected: collection fails because `app.agent.tools` does not exist.

- [ ] **Step 3: Expose duplicate grouping from the finance service**

Add to `app/services/finance.py`:

```python
def find_duplicate_groups(splits: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for split in splits:
        merchant = _merchant(split).casefold()
        amount = _amount(split.get("amount"))
        if merchant and amount is not None:
            groups[(merchant, str(amount.normalize()))].append(split)
    return [group for group in groups.values() if len(group) >= 2]
```

In `app/worker/tasks_sentinel.py`:

```python
from app.services.finance import detect_subscriptions, find_duplicate_groups, format_money
```

Replace both `_duplicate_groups(...)` calls with `find_duplicate_groups(...)`, then delete the old
`_normalize_amount` and `_duplicate_groups` functions. Keep `_normalize_merchant` only if another
sentinel function still uses it; otherwise delete it and its now-unused `InvalidOperation` import.

- [ ] **Step 4: Implement the typed tool registry**

Create empty `app/agent/__init__.py`, then create `app/agent/tools.py`:

```python
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.services.finance import detect_subscriptions, find_duplicate_groups
from app.services.firefly_client import FireflyClient, get_firefly_client


class AgentToolError(RuntimeError):
    pass


class DateRangeInput(BaseModel):
    start: date
    end: date

    @model_validator(mode="after")
    def validate_range(self) -> "DateRangeInput":
        days = (self.end - self.start).days
        if days < 0:
            raise ValueError("结束日期不能早于开始日期")
        if days > 366:
            raise ValueError("查询范围不能超过 366 天")
        return self


class SpendingSummaryInput(DateRangeInput):
    group_by: Literal["category", "merchant"]


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
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {"amount": Decimal(), "count": 0})
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
```

- [ ] **Step 5: Run Agent and sentinel tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_agent.py tests/test_sentinel.py tests/test_finance.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 2**

```powershell
git add app/agent app/services/finance.py app/worker/tasks_sentinel.py tests/test_agent.py
git commit -m "feat: add read-only financial agent tools"
```

### Task 3: Audited three-step Agent runner

**Files:**

- Create: `app/agent/runner.py`
- Test: `tests/test_agent.py`

- [ ] **Step 1: Add failing runner tests**

Append to `tests/test_agent.py`:

```python
from sqlalchemy import select

from app.agent import runner
from app.models.audit import AuditLog
from app.schemas.agent import AgentResponse


class FakeAgentLLM:
    def __init__(self, decisions, final=None):
        self.decisions = list(decisions)
        self.final = final
        self.decision_calls = []
        self.finish_calls = []

    def decide_agent_action(self, question, today, steps):
        self.decision_calls.append((question, today, list(steps)))
        return self.decisions.pop(0)

    def finish_agent_answer(self, question, today, steps):
        self.finish_calls.append((question, today, list(steps)))
        return self.final


def test_runner_executes_observation_loop_and_finishes(db_session, monkeypatch):
    llm = FakeAgentLLM(
        [
            AgentDecision(
                action="summarize_spending",
                arguments={"start": "2026-07-01", "end": "2026-07-27", "group_by": "category"},
                reasoning_summary="先看本月",
            ),
            AgentDecision(
                action="search_transactions",
                arguments={"start": "2026-07-01", "end": "2026-07-27", "category": "餐饮"},
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
    try:
        SpendingSummaryInput.model_validate({"start": "bad"})
    except ValidationError as exc:
        invalid_arguments = exc
    else:
        raise AssertionError("无效工具参数应触发 ValidationError")

    def fake_execute(name, arguments, today=None):
        if name == "bad_tool":
            raise AgentToolError("未注册")
        if name == "search_transactions":
            raise invalid_arguments
        return {"count": 1}

    monkeypatch.setattr(runner, "execute_tool", fake_execute)

    result = runner.run_agent("调查支出", db_session, today=date(2026, 7, 27))

    assert result.stopped_reason == "limit"
    assert len(result.steps) == 3
    assert llm.finish_calls
```

- [ ] **Step 2: Run the runner tests and confirm failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_agent.py -q
```

Expected: import fails because `app.agent.runner` does not exist.

- [ ] **Step 3: Implement the runner**

Create `app/agent/runner.py`:

```python
import json
from datetime import date

import httpx
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.agent.tools import AgentToolError, execute_tool
from app.llm.client import LLMError, get_llm_client
from app.logger import new_trace_id
from app.schemas.agent import AgentResponse, AgentStep, AgentStepSummary
from app.services.firefly_client import FireflyError
from app.services.rules import record_audit

MAX_TOOL_CALLS = 3
_OBSERVATION_SUMMARY_LIMIT = 300


class AgentError(RuntimeError):
    pass


def _audit(session: Session, trace_id: str, event: str, payload: dict) -> None:
    record_audit(session, trace_id, event, payload)
    session.commit()


def _summary(observation: dict) -> str:
    return json.dumps(observation, ensure_ascii=False, default=str)[:_OBSERVATION_SUMMARY_LIMIT]


def _response(
    trace_id: str,
    answer: str,
    stopped_reason: str,
    steps: list[AgentStep],
) -> AgentResponse:
    return AgentResponse(
        trace_id=trace_id,
        answer=answer,
        stopped_reason=stopped_reason,
        steps=[
            AgentStepSummary(
                tool=step.tool,
                arguments=step.arguments,
                status=step.status,
                observation_summary=_summary(step.observation),
            )
            for step in steps
        ],
    )


def run_agent(
    question: str,
    session: Session,
    *,
    today: date | None = None,
) -> AgentResponse:
    current = today or date.today()
    trace_id = new_trace_id()
    steps: list[AgentStep] = []
    llm = get_llm_client()
    _audit(session, trace_id, "agent.started", {"question": question})
    try:
        for _ in range(MAX_TOOL_CALLS):
            decision = llm.decide_agent_action(question, current, steps)
            if decision.action == "finish":
                result = _response(
                    trace_id, decision.final_answer or "信息不足。", "finished", steps
                )
                _audit(
                    session,
                    trace_id,
                    "agent.finished",
                    {"stopped_reason": result.stopped_reason, "steps": len(steps)},
                )
                return result

            _audit(
                session,
                trace_id,
                "agent.tool_called",
                {"tool": decision.action, "arguments": decision.arguments},
            )
            try:
                observation = execute_tool(
                    decision.action, decision.arguments, today=current
                )
                status = "success"
                event = "agent.tool_succeeded"
            except ValidationError as exc:
                observation = {"error": "工具参数无效", "details": str(exc)[:300]}
                status = "validation_error"
                event = "agent.tool_failed"
            except (AgentToolError, FireflyError, httpx.HTTPError) as exc:
                observation = {
                    "error": "工具暂时不可用",
                    "error_type": type(exc).__name__,
                }
                status = "tool_error"
                event = "agent.tool_failed"
            step = AgentStep(
                tool=decision.action,
                arguments=decision.arguments,
                status=status,
                reasoning_summary=decision.reasoning_summary,
                observation=observation,
            )
            steps.append(step)
            _audit(
                session,
                trace_id,
                event,
                {
                    "tool": step.tool,
                    "status": step.status,
                    "observation_summary": _summary(step.observation),
                },
            )

        final = llm.finish_agent_answer(question, current, steps)
        result = _response(trace_id, final.answer, "limit", steps)
        _audit(
            session,
            trace_id,
            "agent.finished",
            {"stopped_reason": result.stopped_reason, "steps": len(steps)},
        )
        return result
    except LLMError as exc:
        _audit(
            session,
            trace_id,
            "agent.failed",
            {"error_type": type(exc).__name__, "steps": len(steps)},
        )
        raise AgentError("AI Agent 服务暂时不可用") from exc
```

- [ ] **Step 4: Run and adjust the focused tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_agent.py -q
```

Expected: all Agent tests pass.

- [ ] **Step 5: Commit Task 3**

```powershell
git add app/agent/runner.py tests/test_agent.py
git commit -m "feat: add audited financial agent loop"
```

### Task 4: FastAPI endpoint and authentication

**Files:**

- Create: `app/api/routes_agent.py`
- Modify: `app/main.py`
- Test: `tests/test_agent.py`

- [ ] **Step 1: Add failing API tests**

Append to `tests/test_agent.py`:

```python
from app.api import routes_agent


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
```

- [ ] **Step 2: Run endpoint tests and confirm failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_agent.py -q
```

Expected: import fails because `app.api.routes_agent` does not exist.

- [ ] **Step 3: Implement the API route**

Create `app/api/routes_agent.py`:

```python
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.agent.runner import AgentError, run_agent
from app.config import get_settings
from app.db import get_session
from app.schemas.agent import AgentQuery, AgentResponse

router = APIRouter(prefix="/agent", tags=["agent"])
SessionDep = Annotated[Session, Depends(get_session)]


def require_agent_auth(
    supplied: Annotated[str | None, Header(alias="X-Console-Token")] = None,
) -> None:
    expected = get_settings().console_token
    if expected and (
        supplied is None
        or not secrets.compare_digest(supplied.encode(), expected.encode())
    ):
        raise HTTPException(status_code=401, detail="缺少或错误的访问令牌")


@router.post(
    "/query",
    response_model=AgentResponse,
    dependencies=[Depends(require_agent_auth)],
)
def agent_query(payload: AgentQuery, session: SessionDep) -> AgentResponse:
    try:
        return run_agent(payload.question, session)
    except AgentError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
```

Modify `app/main.py`:

```python
from app.api.routes_agent import router as agent_router
```

Register it with the other `/api` routers:

```python
    app.include_router(agent_router, prefix="/api")
```

- [ ] **Step 4: Run Agent and API route tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_agent.py tests/test_api_routes.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 4**

```powershell
git add app/api/routes_agent.py app/main.py tests/test_agent.py
git commit -m "feat: expose financial agent API"
```

### Task 5: Documentation and complete verification

**Files:**

- Modify: `README.md`
- Verify: all files changed in Tasks 1–4

- [ ] **Step 1: Document the real Agent behavior**

Add a README section after the architecture diagram:

```markdown
## 多步财务调查 Agent

`POST /api/agent/query` 提供只读财务调查。模型可以在最多三次工具调用内动态选择：

- 按分类或商户汇总支出；
- 查询有限条交易；
- 检测持续订阅与涨价；
- 检测疑似重复扣费。

工具结果会作为 observation 返回模型继续决策。模型没有 SQL、写账和修改分类权限，
金额由本地 `Decimal` 计算，每一步都通过 `trace_id` 记录审计日志。

```bash
curl -X POST http://localhost:8000/api/agent/query \
  -H "Content-Type: application/json" \
  -H "X-Console-Token: $CONSOLE_TOKEN" \
  -d '{"question":"为什么我这个月比上个月花得多？"}'
```

当前 Agent 不包含 RAG、长期记忆和 multi-agent；这些不是完成财务调查所必需的。
```

Also update the feature list and directory tree to mention `app/agent/`, but do not rename the
existing deterministic classification pipeline to an Agent.

- [ ] **Step 2: Run formatting and static checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check .
```

Expected: `All checks passed!`

- [ ] **Step 3: Run the complete test suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: all tests pass; the exact count will be greater than the current 201 tests.

- [ ] **Step 4: Inspect the final diff**

Run:

```powershell
git diff --check
git status --short
git diff --stat
```

Expected:

- `git diff --check` prints nothing.
- Only the planned Agent, shared duplicate helper, tests, main router, LLM client, and README are changed.
- No dependency or migration file is changed.

- [ ] **Step 5: Commit documentation and final polish**

```powershell
git add README.md
git commit -m "docs: explain financial investigation agent"
```

- [ ] **Step 6: Record final evidence**

Run:

```powershell
git status --short
git log --oneline -6
```

Expected: clean worktree and a short sequence of focused Agent commits after design/plan commits.
