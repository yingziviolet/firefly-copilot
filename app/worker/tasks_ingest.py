"""入库管道任务:接入层只落队列,所有业务在这里发生。

ingest_transaction 流程(实现严格按此顺序,每步 record_audit):
1. CanonicalTransaction.load_from_queue(txn_data);bind_trace_id
2. compute_fingerprint -> dedup.claim_fingerprint;None -> 返回 {"result": "duplicate"}
3. classifier.classify
4. 置信度门控:confidence >= settings.confidence_threshold
   - 达标 -> firefly.store_transaction(external_id=指纹) -> mark_status(IMPORTED)
            -> 返回 {"result": "imported", "firefly_id": ...}
   - 不达标 -> review.create_review_item -> mark_status(PENDING_REVIEW)
            -> notifier 提醒待复核 -> 返回 {"result": "pending_review", "item_id": ...}
5. Firefly 写入失败 -> mark_status(FAILED) 后 raise self.retry(最多 3 次,指数退避)

finalize_review(item_id):approved/corrected 的复核项写入 Firefly
(分类取 corrected_category or suggested_category),mark_status(IMPORTED)。

handle_firefly_event(payload, trace_id):P1 只 record_audit 落事件。
"""

from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_scope
from app.logger import bind_trace_id, get_logger
from app.models.ingest import IngestStatus
from app.models.review import ReviewItem, ReviewStatus
from app.schemas.classify import ClassificationResult
from app.schemas.transaction import CanonicalTransaction
from app.services import classifier, dedup, review
from app.services.fingerprint import compute_fingerprint
from app.services.firefly_client import FireflyError, get_firefly_client
from app.services.notifier import notify
from app.services.rules import record_audit
from app.worker.celery_app import celery_app

logger = get_logger(__name__)


def _store_to_firefly(
    task: Any,
    session: Session,
    txn: CanonicalTransaction,
    category: str | None,
    fingerprint: str,
    trace_id: str,
) -> str:
    """写 Firefly;失败先落 FAILED 状态与审计(显式 commit 防止随异常回滚),再触发重试。"""
    try:
        return get_firefly_client().store_transaction(txn, category, external_id=fingerprint)
    except (FireflyError, httpx.HTTPError) as exc:
        dedup.mark_status(session, fingerprint, IngestStatus.FAILED)
        record_audit(
            session,
            trace_id,
            "ingest.failed",
            {"fingerprint": fingerprint, "error": str(exc)},
        )
        session.commit()
        logger.warning("firefly_store_failed", fingerprint=fingerprint, error=str(exc))
        raise task.retry(exc=exc, countdown=2**task.request.retries * 10) from exc


def _notify_pending_review(
    txn: CanonicalTransaction, suggestion: ClassificationResult, item_id: int
) -> None:
    """多通道提醒待复核;通知失败只记日志,不影响管道结果。"""
    text = (
        f"待复核记账:{txn.counterparty} {txn.amount} {txn.currency}\n"
        f"建议分类:{suggestion.category}(置信度 {suggestion.confidence:.2f})\n"
        f"复核项 ID:{item_id}"
    )
    if not notify(text):
        logger.warning("pending_review_notify_failed", item_id=item_id)


@celery_app.task(bind=True, name="app.worker.tasks_ingest.ingest_transaction", max_retries=3)
def ingest_transaction(self, txn_data: dict[str, Any], trace_id: str) -> dict[str, Any]:
    bind_trace_id(trace_id)
    txn = CanonicalTransaction.load_from_queue(txn_data)
    settings = get_settings()

    with session_scope() as session:
        record_audit(
            session,
            trace_id,
            "ingest.received",
            {"source": txn.source.value, "counterparty": txn.counterparty},
        )

        fingerprint = compute_fingerprint(txn)
        record = dedup.claim_fingerprint(session, fingerprint, txn.source.value, trace_id)
        if record is None:
            record_audit(session, trace_id, "ingest.duplicate", {"fingerprint": fingerprint})
            return {"result": "duplicate"}

        suggestion = classifier.classify(session, txn)
        record_audit(
            session,
            trace_id,
            "ingest.classified",
            {
                "fingerprint": fingerprint,
                "category": suggestion.category,
                "confidence": suggestion.confidence,
                "source": suggestion.source,
            },
        )

        if suggestion.confidence >= settings.confidence_threshold:
            firefly_id = _store_to_firefly(
                self, session, txn, suggestion.category, fingerprint, trace_id
            )
            dedup.mark_status(session, fingerprint, IngestStatus.IMPORTED, firefly_id)
            record_audit(
                session,
                trace_id,
                "ingest.imported",
                {"fingerprint": fingerprint, "firefly_id": firefly_id},
            )
            return {"result": "imported", "firefly_id": firefly_id}

        item = review.create_review_item(session, txn, fingerprint, suggestion, trace_id)
        dedup.mark_status(session, fingerprint, IngestStatus.PENDING_REVIEW)
        record_audit(
            session,
            trace_id,
            "ingest.pending_review",
            {"fingerprint": fingerprint, "item_id": item.id},
        )
        _notify_pending_review(txn, suggestion, item.id)
        return {"result": "pending_review", "item_id": item.id}


@celery_app.task(bind=True, name="app.worker.tasks_ingest.finalize_review", max_retries=3)
def finalize_review(self, item_id: int) -> dict[str, Any]:
    with session_scope() as session:
        item = session.get(ReviewItem, item_id)
        if item is None:
            # 契约未定义"不存在":视同不可处理,skipped(重试也救不回来)
            logger.warning("finalize_review_item_missing", item_id=item_id)
            return {"result": "skipped"}

        bind_trace_id(item.trace_id)
        if item.status not in (ReviewStatus.APPROVED, ReviewStatus.CORRECTED):
            logger.info(
                "finalize_review_skipped", item_id=item_id, item_status=item.status.value
            )
            return {"result": "skipped"}

        txn = CanonicalTransaction.load_from_queue(item.txn_payload)
        category = item.corrected_category or item.suggested_category
        firefly_id = _store_to_firefly(
            self, session, txn, category, item.fingerprint, item.trace_id
        )
        dedup.mark_status(session, item.fingerprint, IngestStatus.IMPORTED, firefly_id)
        record_audit(
            session,
            item.trace_id,
            "ingest.imported",
            {"fingerprint": item.fingerprint, "firefly_id": firefly_id, "item_id": item.id},
        )
        return {"result": "imported", "firefly_id": firefly_id}


@celery_app.task(name="app.worker.tasks_ingest.handle_firefly_event")
def handle_firefly_event(payload: dict[str, Any], trace_id: str) -> None:
    bind_trace_id(trace_id)
    with session_scope() as session:
        record_audit(session, trace_id, "webhook.received", payload)
