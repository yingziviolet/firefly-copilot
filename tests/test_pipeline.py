"""入库管道端到端单测:Celery eager + 共享内存库,Firefly/LLM/通知 全部打假。"""

from typing import Any

import pytest
from celery.exceptions import MaxRetriesExceededError, Retry
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.ingest import IngestedTransaction, IngestStatus
from app.models.review import ReviewItem, ReviewStatus
from app.schemas.classify import LLMClassification
from app.schemas.transaction import CanonicalTransaction
from app.services import review
from app.services.firefly_client import FireflyError
from app.services.rules import learn_rule
from app.worker import tasks_ingest

TRACE_ID = "trace-pipeline-01"


def _txn_data(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source": "alipay",
        "direction": "withdrawal",
        "occurred_at": "2026-07-25T12:30:00+08:00",
        "amount": "36.50",
        "currency": "CNY",
        "counterparty": "星巴克",
        "description": "拿铁一杯",
        "source_ref": "alipay-0001",
    }
    base.update(overrides)
    return base


def _fingerprint_of(txn_data: dict[str, Any]) -> str:
    from app.services.fingerprint import compute_fingerprint

    return compute_fingerprint(CanonicalTransaction.load_from_queue(txn_data))


class FakeFirefly:
    """假 Firefly 客户端:记录 store_transaction 调用,可配置抛错。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.exc: Exception | None = None
        self._next_id = 8800

    def store_transaction(self, txn, category, external_id, asset_account=None) -> str:
        self.calls.append(
            {
                "counterparty": txn.counterparty,
                "category": category,
                "external_id": external_id,
            }
        )
        if self.exc is not None:
            raise self.exc
        self._next_id += 1
        return str(self._next_id)


@pytest.fixture()
def fake_firefly(monkeypatch) -> FakeFirefly:
    fake = FakeFirefly()
    monkeypatch.setattr(tasks_ingest, "get_firefly_client", lambda: fake)
    return fake


@pytest.fixture()
def notify_calls(monkeypatch) -> list[str]:
    calls: list[str] = []

    def fake_notify(text: str, parse_mode: str | None = None) -> bool:
        calls.append(text)
        return True

    monkeypatch.setattr(tasks_ingest, "notify", fake_notify)
    return calls


@pytest.fixture()
def llm_guard(monkeypatch) -> None:
    """规则命中路径不允许触达 LLM:一旦调用直接炸测试。"""

    def _boom():
        raise AssertionError("LLM 不应被调用")

    monkeypatch.setattr("app.services.classifier.get_llm_client", _boom)


@pytest.fixture()
def low_confidence_llm(monkeypatch) -> None:
    """假 LLM:固定返回 0.5 置信度,必进人工复核(阈值 0.9)。"""

    class FakeLLM:
        def classify_transaction(self, txn, categories) -> LLMClassification:
            return LLMClassification(category="餐饮", confidence=0.5, rationale="测试假 LLM")

    monkeypatch.setattr("app.services.classifier.get_llm_client", lambda: FakeLLM())


def _seed_rule(db_session, merchant: str = "星巴克", category: str = "餐饮") -> None:
    learn_rule(db_session, merchant, category)
    db_session.commit()


def test_high_confidence_rule_hit_imported(db_session, celery_eager, fake_firefly, llm_guard):
    _seed_rule(db_session)
    txn_data = _txn_data()

    result = tasks_ingest.ingest_transaction.delay(txn_data, TRACE_ID).get()

    assert result["result"] == "imported"
    assert result["firefly_id"] == "8801"

    record = db_session.execute(select(IngestedTransaction)).scalar_one()
    assert record.status == IngestStatus.IMPORTED
    assert record.firefly_transaction_id == "8801"
    assert record.trace_id == TRACE_ID

    # Firefly 收到的 external_id 必须是指纹
    assert len(fake_firefly.calls) == 1
    assert fake_firefly.calls[0]["external_id"] == _fingerprint_of(txn_data)
    assert fake_firefly.calls[0]["category"] == "餐饮"

    # 审计链路完整
    events = list(db_session.scalars(select(AuditLog.event).order_by(AuditLog.id)))
    assert events == ["ingest.received", "ingest.classified", "ingest.imported"]


def test_same_txn_twice_is_duplicate_and_firefly_called_once(
    db_session, celery_eager, fake_firefly, llm_guard
):
    _seed_rule(db_session)
    txn_data = _txn_data()

    first = tasks_ingest.ingest_transaction.delay(txn_data, TRACE_ID).get()
    second = tasks_ingest.ingest_transaction.delay(txn_data, "trace-pipeline-02").get()

    assert first["result"] == "imported"
    assert second == {"result": "duplicate"}
    # 幂等核心:同一指纹只写一次 Firefly、只落一条登记
    assert len(fake_firefly.calls) == 1
    records = list(db_session.scalars(select(IngestedTransaction)))
    assert len(records) == 1
    assert records[0].status == IngestStatus.IMPORTED

    dup_events = list(
        db_session.scalars(select(AuditLog).where(AuditLog.event == "ingest.duplicate"))
    )
    assert len(dup_events) == 1
    assert dup_events[0].trace_id == "trace-pipeline-02"


def test_low_confidence_goes_to_pending_review(
    db_session, celery_eager, fake_firefly, low_confidence_llm, notify_calls
):
    txn_data = _txn_data(counterparty="无名小店", source_ref="alipay-0002")

    result = tasks_ingest.ingest_transaction.delay(txn_data, TRACE_ID).get()

    assert result["result"] == "pending_review"

    item = db_session.execute(select(ReviewItem)).scalar_one()
    assert item.id == result["item_id"]
    assert item.status == ReviewStatus.PENDING
    assert item.suggested_category == "餐饮"
    assert item.confidence == 0.5
    assert item.fingerprint == _fingerprint_of(txn_data)

    record = db_session.execute(select(IngestedTransaction)).scalar_one()
    assert record.status == IngestStatus.PENDING_REVIEW
    # 低置信度不得写 Firefly;需发出待复核提醒
    assert fake_firefly.calls == []
    assert len(notify_calls) == 1
    assert "无名小店" in notify_calls[0]

    events = list(db_session.scalars(select(AuditLog.event).order_by(AuditLog.id)))
    assert events == ["ingest.received", "ingest.classified", "ingest.pending_review"]


def test_firefly_failure_marks_failed_and_retries(
    db_session, celery_eager, fake_firefly, llm_guard
):
    _seed_rule(db_session)
    fake_firefly.exc = FireflyError("Firefly API POST failed: HTTP 500, body='boom'")

    with pytest.raises((Retry, FireflyError, MaxRetriesExceededError)):
        tasks_ingest.ingest_transaction.delay(_txn_data(), TRACE_ID)

    record = db_session.execute(select(IngestedTransaction)).scalar_one()
    assert record.status == IngestStatus.FAILED
    assert record.firefly_transaction_id is None
    # 失败也要留审计
    failed = list(db_session.scalars(select(AuditLog).where(AuditLog.event == "ingest.failed")))
    assert len(failed) == 1
    assert "boom" in failed[0].payload["error"]


def test_finalize_review_after_approve_imports(
    db_session, celery_eager, fake_firefly, low_confidence_llm, notify_calls
):
    txn_data = _txn_data(counterparty="无名小店", source_ref="alipay-0003")
    pending = tasks_ingest.ingest_transaction.delay(txn_data, TRACE_ID).get()
    item_id = pending["item_id"]
    assert fake_firefly.calls == []

    # pending 状态直接 finalize -> skipped,且不碰 Firefly
    assert tasks_ingest.finalize_review.delay(item_id).get() == {"result": "skipped"}
    assert fake_firefly.calls == []

    review.approve(db_session, item_id)
    db_session.commit()

    result = tasks_ingest.finalize_review.delay(item_id).get()

    assert result["result"] == "imported"
    assert result["firefly_id"] == "8801"
    assert len(fake_firefly.calls) == 1
    # 分类取 corrected_category or suggested_category(approve 场景即建议分类)
    assert fake_firefly.calls[0]["category"] == "餐饮"
    assert fake_firefly.calls[0]["external_id"] == _fingerprint_of(txn_data)

    record = db_session.execute(select(IngestedTransaction)).scalar_one()
    assert record.status == IngestStatus.IMPORTED
    assert record.firefly_transaction_id == "8801"


def test_finalize_review_corrected_uses_corrected_category(
    db_session, celery_eager, fake_firefly, low_confidence_llm, notify_calls
):
    txn_data = _txn_data(counterparty="盒马鲜生", source_ref="alipay-0004")
    pending = tasks_ingest.ingest_transaction.delay(txn_data, TRACE_ID).get()
    item_id = pending["item_id"]

    review.correct(db_session, item_id, "日用")
    db_session.commit()

    result = tasks_ingest.finalize_review.delay(item_id).get()

    assert result["result"] == "imported"
    assert fake_firefly.calls[0]["category"] == "日用"
    record = db_session.execute(select(IngestedTransaction)).scalar_one()
    assert record.status == IngestStatus.IMPORTED


def test_finalize_review_rejected_skipped(
    db_session, celery_eager, fake_firefly, low_confidence_llm, notify_calls
):
    txn_data = _txn_data(counterparty="无名小店", source_ref="alipay-0005")
    pending = tasks_ingest.ingest_transaction.delay(txn_data, TRACE_ID).get()
    item_id = pending["item_id"]

    review.reject(db_session, item_id)
    db_session.commit()

    assert tasks_ingest.finalize_review.delay(item_id).get() == {"result": "skipped"}
    assert fake_firefly.calls == []


def test_handle_firefly_event_records_audit(db_session, celery_eager):
    payload = {"trigger": "STORE_TRANSACTION", "content": {"id": 1}}

    tasks_ingest.handle_firefly_event.delay(payload, TRACE_ID).get()

    log = db_session.execute(
        select(AuditLog).where(AuditLog.event == "webhook.received")
    ).scalar_one()
    assert log.trace_id == TRACE_ID
    assert log.payload == payload
