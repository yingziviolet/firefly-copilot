"""规则库:商户 -> 分类。命中即免 LLM,用户改正自动回流。"""

import enum

from sqlalchemy import Boolean, Enum, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class RuleMatchType(enum.StrEnum):
    EXACT = "exact"
    CONTAINS = "contains"
    REGEX = "regex"


class RuleOrigin(enum.StrEnum):
    MANUAL = "manual"
    CORRECTION = "correction"  # 人工复核改正回流
    SEED = "seed"


class Rule(TimestampMixin, Base):
    __tablename__ = "rules"
    __table_args__ = (UniqueConstraint("merchant_pattern", "match_type", name="uq_rule_pattern"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    merchant_pattern: Mapped[str] = mapped_column(String(255), index=True)
    match_type: Mapped[RuleMatchType] = mapped_column(
        Enum(RuleMatchType, native_enum=False, length=16), default=RuleMatchType.EXACT
    )
    category: Mapped[str] = mapped_column(String(100))
    budget: Mapped[str | None] = mapped_column(String(100), default=None)
    origin: Mapped[RuleOrigin] = mapped_column(
        Enum(RuleOrigin, native_enum=False, length=16), default=RuleOrigin.MANUAL
    )
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
