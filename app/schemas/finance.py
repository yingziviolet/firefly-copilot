from datetime import date
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator


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
        return {"expense": "withdrawal", "income": "deposit"}.get(value, value)

    @model_validator(mode="after")
    def validate_range(self) -> "FinanceQuery":
        days = (self.end - self.start).days
        if days < 0:
            raise ValueError("结束日期不能早于开始日期")
        if days > 366:
            raise ValueError("查询范围不能超过 366 天")
        return self
