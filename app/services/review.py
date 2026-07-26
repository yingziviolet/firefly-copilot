"""人工复核服务:状态机 pending -> approved/corrected/rejected。

改正(corrected)必须调用 rules.learn_rule 回流规则库。
实际写入 Firefly 由 worker 任务 finalize_review 执行,这里只做状态流转。
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logger import get_logger
from app.models.review import ReviewItem, ReviewStatus
from app.schemas.classify import ClassificationResult
from app.schemas.transaction import CanonicalTransaction
from app.services import rules

logger = get_logger(__name__)


def _get_pending_item(session: Session, item_id: int) -> ReviewItem:
    """取出待流转的复核项;不存在或非 pending 均抛 ValueError。"""
    item = session.get(ReviewItem, item_id)
    if item is None:
        raise ValueError(f"复核项不存在: id={item_id}")
    if item.status != ReviewStatus.PENDING:
        raise ValueError(f"复核项 {item_id} 状态为 {item.status.value},仅 pending 可流转")
    return item


def _resolve(item: ReviewItem, status: ReviewStatus) -> None:
    item.status = status
    item.resolved_at = datetime.now(UTC)


def create_review_item(
    session: Session,
    txn: CanonicalTransaction,
    fingerprint: str,
    suggestion: ClassificationResult,
    trace_id: str,
) -> ReviewItem:
    item = ReviewItem(
        fingerprint=fingerprint,
        txn_payload=txn.dump_for_queue(),
        suggested_category=suggestion.category,
        confidence=suggestion.confidence,
        status=ReviewStatus.PENDING,
        trace_id=trace_id,
    )
    session.add(item)
    session.flush()
    logger.info(
        "review_item_created",
        item_id=item.id,
        fingerprint=fingerprint,
        suggested_category=suggestion.category,
        confidence=suggestion.confidence,
    )
    return item


def approve(session: Session, item_id: int) -> ReviewItem:
    """pending -> approved;非 pending 抛 ValueError。"""
    item = _get_pending_item(session, item_id)
    _resolve(item, ReviewStatus.APPROVED)
    session.flush()
    logger.info("review_approved", item_id=item.id, category=item.suggested_category)
    return item


def correct(session: Session, item_id: int, category: str) -> ReviewItem:
    """pending -> corrected,记录 corrected_category 并 learn_rule 回流。"""
    item = _get_pending_item(session, item_id)
    item.corrected_category = category
    _resolve(item, ReviewStatus.CORRECTED)
    # 回流规则库:商户取入队时保存的 counterparty
    merchant = (item.txn_payload or {}).get("counterparty", "")
    if merchant:
        rules.learn_rule(session, merchant, category)
    else:
        logger.warning("review_correct_no_counterparty", item_id=item.id)
    session.flush()
    logger.info("review_corrected", item_id=item.id, category=category)
    return item


def reject(session: Session, item_id: int) -> ReviewItem:
    item = _get_pending_item(session, item_id)
    _resolve(item, ReviewStatus.REJECTED)
    session.flush()
    logger.info("review_rejected", item_id=item.id)
    return item


def list_pending(session: Session, limit: int = 10) -> list[ReviewItem]:
    return list(
        session.scalars(
            select(ReviewItem)
            .where(ReviewItem.status == ReviewStatus.PENDING)
            .order_by(ReviewItem.id)
            .limit(limit)
        ).all()
    )
