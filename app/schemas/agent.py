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
