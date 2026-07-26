"""统一交易模型:所有渠道(CSV/快捷记账/webhook)解析后都收敛到 CanonicalTransaction。"""

import enum
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TxnSource(enum.StrEnum):
    ALIPAY = "alipay"
    WECHAT = "wechat"
    MANUAL = "manual"
    WEBHOOK = "webhook"


class TxnDirection(enum.StrEnum):
    """取值对齐 Firefly III 的 transaction type。"""

    EXPENSE = "withdrawal"
    INCOME = "deposit"
    TRANSFER = "transfer"


class CanonicalTransaction(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    source: TxnSource
    direction: TxnDirection
    occurred_at: datetime
    amount: Decimal = Field(gt=0, description="金额恒为正,方向由 direction 表达")
    currency: str = "CNY"
    counterparty: str = Field(description="商户/交易对方")
    description: str = ""
    source_ref: str | None = Field(default=None, description="渠道原始单号,如支付宝交易号")
    category_hint: str | None = Field(default=None, description="渠道自带的分类提示")
    account_hint: str | None = Field(default=None, description="支付方式/账户提示")
    raw: dict[str, Any] | None = Field(default=None, description="原始行,审计用")

    @field_validator("amount", mode="before")
    @classmethod
    def _clean_amount(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.replace("¥", "").replace("￥", "").replace(",", "").strip()
        return v

    def dump_for_queue(self) -> dict[str, Any]:
        """入 Celery 队列的 JSON 安全序列化。"""
        return self.model_dump(mode="json")

    @classmethod
    def load_from_queue(cls, data: dict[str, Any]) -> "CanonicalTransaction":
        return cls.model_validate(data)
