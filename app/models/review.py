"""人工复核队列:低置信度交易在这里等待 Telegram 按钮裁决。"""

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ReviewStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"  # 按建议分类入库
    CORRECTED = "corrected"  # 改分类后入库,回流规则库
    REJECTED = "rejected"


class ReviewItem(TimestampMixin, Base):
    __tablename__ = "review_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    txn_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    suggested_category: Mapped[str | None] = mapped_column(String(100), default=None)
    confidence: Mapped[float | None] = mapped_column(Float, default=None)
    status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, native_enum=False, length=16), default=ReviewStatus.PENDING, index=True
    )
    corrected_category: Mapped[str | None] = mapped_column(String(100), default=None)
    telegram_message_id: Mapped[str | None] = mapped_column(String(64), default=None)
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
