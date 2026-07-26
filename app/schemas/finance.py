from datetime import date
from typing import Literal

from pydantic import BaseModel, model_validator


class FinanceQuery(BaseModel):
    start: date
    end: date
    transaction_type: Literal["withdrawal", "deposit"] = "withdrawal"
    category: str | None = None
    merchant: str | None = None
    metric: Literal["sum", "count"] = "sum"

    @model_validator(mode="after")
    def validate_range(self) -> "FinanceQuery":
        days = (self.end - self.start).days
        if days < 0:
            raise ValueError("结束日期不能早于开始日期")
        if days > 366:
            raise ValueError("查询范围不能超过 366 天")
        return self
