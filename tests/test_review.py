"""review 服务单测:create/approve/correct(规则回流)/reject/重复流转/list_pending。"""

from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.review import ReviewItem, ReviewStatus
from app.models.rule import Rule, RuleMatchType, RuleOrigin
from app.schemas.classify import ClassificationResult
from app.schemas.transaction import CanonicalTransaction, TxnDirection, TxnSource
from app.services import review


def make_txn(counterparty: str = "星巴克") -> CanonicalTransaction:
    return CanonicalTransaction(
        source=TxnSource.ALIPAY,
        direction=TxnDirection.EXPENSE,
        occurred_at=datetime(2026, 7, 25, 12, 30),
        amount=Decimal("35.00"),
        counterparty=counterparty,
        description="咖啡",
    )


def make_suggestion(category: str = "餐饮", confidence: float = 0.6) -> ClassificationResult:
    return ClassificationResult(category=category, confidence=confidence, source="llm")


def create_item(session, counterparty: str = "星巴克") -> ReviewItem:
    return review.create_review_item(
        session,
        make_txn(counterparty),
        fingerprint=f"fp-{counterparty}",
        suggestion=make_suggestion(),
        trace_id="trace-001",
    )


def test_create_review_item_persists_fields(db_session):
    item = create_item(db_session)
    assert item.id is not None
    assert item.status == ReviewStatus.PENDING
    assert item.fingerprint == "fp-星巴克"
    assert item.suggested_category == "餐饮"
    assert item.confidence == 0.6
    assert item.trace_id == "trace-001"
    assert item.resolved_at is None
    assert item.corrected_category is None
    # txn_payload 为 JSON 安全序列化后的交易
    assert item.txn_payload["counterparty"] == "星巴克"
    assert item.txn_payload["amount"] == "35.00"
    assert db_session.get(ReviewItem, item.id) is item


def test_approve_sets_status_and_resolved_at(db_session):
    item = create_item(db_session)
    result = review.approve(db_session, item.id)
    assert result is item
    assert result.status == ReviewStatus.APPROVED
    assert result.resolved_at is not None
    # 不回流规则库
    assert db_session.scalars(select(Rule)).all() == []


def test_correct_records_category_and_learns_exact_rule(db_session):
    item = create_item(db_session, counterparty="  StarBucks  ")
    result = review.correct(db_session, item.id, "咖啡饮品")
    assert result.status == ReviewStatus.CORRECTED
    assert result.corrected_category == "咖啡饮品"
    assert result.resolved_at is not None

    # 规则库新增一条 exact 规则(商户归一化、来源 correction)
    learned = db_session.scalars(select(Rule)).all()
    assert len(learned) == 1
    rule = learned[0]
    assert rule.match_type == RuleMatchType.EXACT
    assert rule.merchant_pattern == "starbucks"
    assert rule.category == "咖啡饮品"
    assert rule.origin == RuleOrigin.CORRECTION


def test_reject_sets_status(db_session):
    item = create_item(db_session)
    result = review.reject(db_session, item.id)
    assert result.status == ReviewStatus.REJECTED
    assert result.resolved_at is not None
    assert db_session.scalars(select(Rule)).all() == []


@pytest.mark.parametrize(
    "first, second",
    [
        (review.approve, review.approve),
        (review.approve, review.reject),
        (review.reject, review.approve),
    ],
)
def test_transition_from_non_pending_raises(db_session, first, second):
    item = create_item(db_session)
    first(db_session, item.id)
    with pytest.raises(ValueError):
        second(db_session, item.id)


def test_correct_after_correct_raises(db_session):
    item = create_item(db_session)
    review.correct(db_session, item.id, "餐饮")
    with pytest.raises(ValueError):
        review.correct(db_session, item.id, "购物")


def test_transition_missing_item_raises(db_session):
    with pytest.raises(ValueError):
        review.approve(db_session, 9999)


def test_list_pending_only_contains_pending(db_session):
    kept = create_item(db_session, counterparty="商户A")
    approved = create_item(db_session, counterparty="商户B")
    rejected = create_item(db_session, counterparty="商户C")
    review.approve(db_session, approved.id)
    review.reject(db_session, rejected.id)

    pending = review.list_pending(db_session)
    assert [i.id for i in pending] == [kept.id]
    assert all(i.status == ReviewStatus.PENDING for i in pending)


def test_list_pending_respects_limit(db_session):
    items = [create_item(db_session, counterparty=f"商户{i}") for i in range(5)]
    pending = review.list_pending(db_session, limit=3)
    # 按 id 升序取前 limit 条
    assert [i.id for i in pending] == [items[0].id, items[1].id, items[2].id]
