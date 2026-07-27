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
    return json.dumps(observation, ensure_ascii=False, default=str)[
        :_OBSERVATION_SUMMARY_LIMIT
    ]


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
                    trace_id,
                    decision.final_answer or "信息不足。",
                    "finished",
                    steps,
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
                    decision.action,
                    decision.arguments,
                    today=current,
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
