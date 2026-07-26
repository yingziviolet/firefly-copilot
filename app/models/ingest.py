"""去重指纹登记表:同一笔交易(CSV 重复导入/webhook 重放/任务重试)只入库一次。"""

import enum

from sqlalchemy import Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class IngestStatus(enum.StrEnum):
    RECEIVED = "received"
    IMPORTED = "imported"
    DUPLICATE = "duplicate"
    PENDING_REVIEW = "pending_review"
    REJECTED = "rejected"
    FAILED = "failed"


class IngestedTransaction(TimestampMixin, Base):
    __tablename__ = "ingested_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(16))
    status: Mapped[IngestStatus] = mapped_column(
        Enum(IngestStatus, native_enum=False, length=16), default=IngestStatus.RECEIVED
    )
    firefly_transaction_id: Mapped[str | None] = mapped_column(String(32), default=None)
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
