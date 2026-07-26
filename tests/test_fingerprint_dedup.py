"""fingerprint + dedup 单元测试(用 conftest 的 db_session fixture)。"""

import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.models.ingest import IngestedTransaction, IngestStatus
from app.schemas.transaction import CanonicalTransaction, TxnDirection, TxnSource
from app.services.dedup import claim_fingerprint, mark_status
from app.services.fingerprint import compute_fingerprint


def make_txn(**overrides: Any) -> CanonicalTransaction:
    data: dict[str, Any] = {
        "source": TxnSource.ALIPAY,
        "direction": TxnDirection.EXPENSE,
        "occurred_at": datetime(2026, 7, 1, 12, 30, 0),
        "amount": Decimal("12.50"),
        "counterparty": "Starbucks",
        "source_ref": None,
    }
    data.update(overrides)
    return CanonicalTransaction(**data)


# ---------- 指纹计算 ----------


def test_fingerprint_deterministic():
    fp1 = compute_fingerprint(make_txn())
    fp2 = compute_fingerprint(make_txn())
    assert fp1 == fp2
    assert len(fp1) == 64
    int(fp1, 16)  # 必须是合法十六进制


def test_different_txn_different_fingerprint():
    base = compute_fingerprint(make_txn())
    assert compute_fingerprint(make_txn(amount=Decimal("13.50"))) != base
    assert compute_fingerprint(make_txn(counterparty="Luckin")) != base
    assert compute_fingerprint(make_txn(direction=TxnDirection.INCOME)) != base
    assert compute_fingerprint(make_txn(occurred_at=datetime(2026, 7, 2, 12, 30))) != base


def test_normalization_fullwidth_and_case_equivalent():
    # 全角字母、大小写差异归一化后指纹相同
    fp_half = compute_fingerprint(make_txn(counterparty="starbucks"))
    fp_full = compute_fingerprint(make_txn(counterparty="Ｓｔａｒｂｕｃｋｓ"))
    fp_upper = compute_fingerprint(make_txn(counterparty="STARBUCKS"))
    assert fp_full == fp_half
    assert fp_upper == fp_half


def test_normalization_strips_whitespace():
    # model_copy(update=...) 不触发 Pydantic 的 strip,专门验证指纹函数自身的去空白
    base = make_txn(counterparty="starbucks")
    padded = base.model_copy(update={"counterparty": "  starbucks　 "})
    assert compute_fingerprint(padded) == compute_fingerprint(base)


def test_amount_two_decimal_formatting():
    # 12.5 与 12.50 按两位小数格式化后指纹一致
    fp_a = compute_fingerprint(make_txn(amount=Decimal("12.5")))
    fp_b = compute_fingerprint(make_txn(amount=Decimal("12.50")))
    assert fp_a == fp_b


def test_source_ref_path_takes_priority():
    txn = make_txn(source_ref="2026070112345678")
    expected = hashlib.sha256(b"alipay:2026070112345678").hexdigest()
    assert compute_fingerprint(txn) == expected
    # 有 source_ref 时其余字段不参与指纹
    changed = make_txn(
        source_ref="2026070112345678", amount=Decimal("99.99"), counterparty="Other"
    )
    assert compute_fingerprint(changed) == expected
    # 同一单号不同渠道属于不同指纹
    other_source = make_txn(source=TxnSource.WECHAT, source_ref="2026070112345678")
    assert compute_fingerprint(other_source) != expected


def test_no_source_ref_formula_exact():
    txn = make_txn()
    expected = hashlib.sha256(b"2026-07-01|12.50|starbucks|withdrawal").hexdigest()
    assert compute_fingerprint(txn) == expected


# ---------- 去重登记 ----------


def test_claim_first_success_then_duplicate_none(db_session):
    fp = "a" * 64
    rec = claim_fingerprint(db_session, fp, "alipay", "trace-001")
    assert rec is not None
    assert rec.status is IngestStatus.RECEIVED
    assert rec.fingerprint == fp
    assert rec.source == "alipay"
    assert rec.trace_id == "trace-001"

    dup = claim_fingerprint(db_session, fp, "alipay", "trace-002")
    assert dup is None

    # 重复 claim 的回滚不能吞掉首次登记
    kept = db_session.execute(
        select(IngestedTransaction).where(IngestedTransaction.fingerprint == fp)
    ).scalar_one()
    assert kept.trace_id == "trace-001"


def test_claim_distinct_fingerprints_both_succeed(db_session):
    rec1 = claim_fingerprint(db_session, "b" * 64, "alipay", "t1")
    rec2 = claim_fingerprint(db_session, "c" * 64, "wechat", "t2")
    assert rec1 is not None and rec2 is not None
    assert rec1.id != rec2.id


def test_mark_status_updates_record(db_session):
    fp = "d" * 64
    claim_fingerprint(db_session, fp, "alipay", "t3")

    updated = mark_status(db_session, fp, IngestStatus.IMPORTED, firefly_transaction_id="42")
    assert updated is not None
    assert updated.status is IngestStatus.IMPORTED
    assert updated.firefly_transaction_id == "42"

    # 再查一遍确认已落到会话/数据库
    row = db_session.execute(
        select(IngestedTransaction).where(IngestedTransaction.fingerprint == fp)
    ).scalar_one()
    assert row.status is IngestStatus.IMPORTED
    assert row.firefly_transaction_id == "42"

    # 不传 firefly_transaction_id 时只改状态、不清空已有 ID
    again = mark_status(db_session, fp, IngestStatus.PENDING_REVIEW)
    assert again is not None
    assert again.status is IngestStatus.PENDING_REVIEW
    assert again.firefly_transaction_id == "42"


def test_mark_status_missing_fingerprint_returns_none(db_session):
    assert mark_status(db_session, "f" * 64, IngestStatus.FAILED) is None
