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


def _seed_pending(db_session, celery_eager, counterparty: str, source_ref: str) -> int:
    """经低置信度 LLM 入一条 pending 复核项,返回 item_id。"""
    result = tasks_ingest.ingest_transaction.delay(
        _txn_data(counterparty=counterparty, source_ref=source_ref), TRACE_ID
    ).get()
    assert result["result"] == "pending_review"
    return result["item_id"]


def test_reclassify_pending_llm_mode(
    db_session, celery_eager, fake_firefly, low_confidence_llm, notify_calls, monkeypatch
):
    """rules_only=False:LLM 高置信度项自动批准入库,低置信度只刷新建议。"""
    high_id = _seed_pending(db_session, celery_eager, "高分店", "rc-01")
    low_id = _seed_pending(db_session, celery_eager, "低分店", "rc-02")
    assert fake_firefly.calls == []

    class VarLLM:
        def classify_transaction(self, txn, categories) -> LLMClassification:
            if txn.counterparty == "高分店":
                return LLMClassification(category="购物", confidence=0.95, rationale="高")
            return LLMClassification(category="娱乐", confidence=0.4, rationale="低")

    monkeypatch.setattr("app.services.classifier.get_llm_client", lambda: VarLLM())

    result = tasks_ingest.reclassify_pending.delay(rules_only=False).get()
    assert result == {"resolved": 1, "remaining": 1}

    high = db_session.get(ReviewItem, high_id)
    assert high.status == ReviewStatus.APPROVED
    assert high.suggested_category == "购物"
    assert high.confidence == 0.95
    # 达标项按 finalize 逻辑写入 Firefly 并落 IMPORTED
    assert len(fake_firefly.calls) == 1
    assert fake_firefly.calls[0]["category"] == "购物"
    record = db_session.execute(
        select(IngestedTransaction).where(IngestedTransaction.fingerprint == high.fingerprint)
    ).scalar_one()
    assert record.status == IngestStatus.IMPORTED

    # 不达标项保持 pending,但建议已刷新
    low = db_session.get(ReviewItem, low_id)
    assert low.status == ReviewStatus.PENDING
    assert low.suggested_category == "娱乐"
    assert low.confidence == 0.4


def test_reclassify_pending_rules_only(
    db_session, celery_eager, fake_firefly, low_confidence_llm, notify_calls, monkeypatch
):
    """rules_only=True:仅规则命中项流转,未命中跳过且不触达 LLM。"""
    hit_id = _seed_pending(db_session, celery_eager, "规则店", "rc-03")
    miss_id = _seed_pending(db_session, celery_eager, "无规则店", "rc-04")
    _seed_rule(db_session, merchant="规则店", category="日用")

    llm_calls: list[int] = []

    def _tracking_llm():
        llm_calls.append(1)
        raise AssertionError("rules_only 不应触达 LLM")

    monkeypatch.setattr("app.services.classifier.get_llm_client", _tracking_llm)

    result = tasks_ingest.reclassify_pending.delay(rules_only=True).get()
    assert result == {"resolved": 1, "remaining": 1}
    assert llm_calls == []  # 零 LLM 成本

    hit = db_session.get(ReviewItem, hit_id)
    assert hit.status == ReviewStatus.APPROVED
    assert hit.suggested_category == "日用"
    assert hit.confidence == 1.0
    assert len(fake_firefly.calls) == 1
    assert fake_firefly.calls[0]["category"] == "日用"

    miss = db_session.get(ReviewItem, miss_id)
    assert miss.status == ReviewStatus.PENDING
    assert miss.suggested_category == "餐饮"  # 原建议原样保留


def test_reclassify_single_failure_does_not_abort(
    db_session, celery_eager, fake_firefly, low_confidence_llm, notify_calls, monkeypatch
):
    """坏 payload 单条失败:记日志跳过,其余照常处理。"""
    bad_id = _seed_pending(db_session, celery_eager, "坏数据店", "rc-05")
    good_id = _seed_pending(db_session, celery_eager, "好数据店", "rc-06")
    bad = db_session.get(ReviewItem, bad_id)
    bad.txn_payload = {"bad": "payload"}
    db_session.commit()

    class HighLLM:
        def classify_transaction(self, txn, categories) -> LLMClassification:
            return LLMClassification(category="购物", confidence=0.95, rationale="高")

    monkeypatch.setattr("app.services.classifier.get_llm_client", lambda: HighLLM())

    result = tasks_ingest.reclassify_pending.delay(rules_only=False).get()
    assert result == {"resolved": 1, "remaining": 1}
    assert db_session.get(ReviewItem, good_id).status == ReviewStatus.APPROVED
    assert db_session.get(ReviewItem, bad_id).status == ReviewStatus.PENDING


def test_handle_firefly_event_records_audit(db_session, celery_eager):
    payload = {"trigger": "STORE_TRANSACTION", "content": {"id": 1}}

    tasks_ingest.handle_firefly_event.delay(payload, TRACE_ID).get()

    log = db_session.execute(
        select(AuditLog).where(AuditLog.event == "webhook.received")
    ).scalar_one()
    assert log.trace_id == TRACE_ID
    assert log.payload == payload
