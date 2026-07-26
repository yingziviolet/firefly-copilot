"""去重登记:靠 ingested_transactions.fingerprint 唯一约束保证并发安全。"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.logger import get_logger
from app.models.ingest import IngestedTransaction, IngestStatus

logger = get_logger(__name__)


def claim_fingerprint(
    session: Session, fingerprint: str, source: str, trace_id: str
) -> IngestedTransaction | None:
    """尝试登记指纹。成功返回新记录(status=RECEIVED);已存在返回 None(重复)。

    实现要求:INSERT 后 flush 捕获 IntegrityError 判重(并发安全),
    捕获后需 rollback 到干净状态再返回 None。
    """
    record = IngestedTransaction(
        fingerprint=fingerprint,
        source=source,
        status=IngestStatus.RECEIVED,
        trace_id=trace_id,
    )
    try:
        # SAVEPOINT 内 INSERT+flush:撞唯一约束时只回滚本次插入,
        # 不破坏外层事务里已完成的工作(如批量导入中已登记的其他行)
        with session.begin_nested():
            session.add(record)
            session.flush()
    except IntegrityError:
        # begin_nested 上下文退出时已回滚 SAVEPOINT,会话回到干净状态
        logger.info("fingerprint_duplicate", fingerprint=fingerprint, source=source)
        return None
    return record


def mark_status(
    session: Session,
    fingerprint: str,
    status: IngestStatus,
    firefly_transaction_id: str | None = None,
) -> IngestedTransaction | None:
    """更新指纹记录状态(入库成功/进复核/失败等)。"""
    record = session.execute(
        select(IngestedTransaction).where(IngestedTransaction.fingerprint == fingerprint)
    ).scalar_one_or_none()
    if record is None:
        logger.warning("fingerprint_not_found", fingerprint=fingerprint)
        return None
    record.status = status
    if firefly_transaction_id is not None:
        record.firefly_transaction_id = firefly_transaction_id
    session.flush()
    return record
