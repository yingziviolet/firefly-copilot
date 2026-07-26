"""rules 服务单测:三种匹配与优先级、disabled、hit_count、learn_rule upsert、审计落库。"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.rule import Rule, RuleMatchType, RuleOrigin
from app.schemas.transaction import CanonicalTransaction, TxnDirection, TxnSource
from app.services import rules


def make_txn(counterparty: str) -> CanonicalTransaction:
    return CanonicalTransaction(
        source=TxnSource.MANUAL,
        direction=TxnDirection.EXPENSE,
        occurred_at=datetime(2026, 7, 1, 12, 0, 0),
        amount=Decimal("25.00"),
        counterparty=counterparty,
    )


def add_rule(
    session,
    pattern: str,
    match_type: RuleMatchType = RuleMatchType.EXACT,
    category: str = "餐饮",
    enabled: bool = True,
) -> Rule:
    rule = Rule(
        merchant_pattern=pattern, match_type=match_type, category=category, enabled=enabled
    )
    session.add(rule)
    session.flush()
    return rule


def test_exact_match_uses_normalized_counterparty(db_session):
    rule = add_rule(db_session, "starbucks", RuleMatchType.EXACT)
    # 大小写与首尾空白都应被归一化掉
    hit = rules.match_rule(db_session, make_txn("  StarBucks  "))
    assert hit is not None
    assert hit.id == rule.id
    assert hit.hit_count == 1


def test_exact_does_not_match_substring(db_session):
    add_rule(db_session, "星巴克", RuleMatchType.EXACT)
    assert rules.match_rule(db_session, make_txn("星巴克咖啡有限公司")) is None


def test_contains_match(db_session):
    rule = add_rule(db_session, "美团", RuleMatchType.CONTAINS, category="外卖")
    hit = rules.match_rule(db_session, make_txn("美团外卖-订单12345"))
    assert hit is not None
    assert hit.id == rule.id


def test_regex_match(db_session):
    rule = add_rule(db_session, r"滴滴|uber", RuleMatchType.REGEX, category="出行")
    hit = rules.match_rule(db_session, make_txn("滴滴出行科技"))
    assert hit is not None
    assert hit.id == rule.id
    # 正则对归一化(小写)后的文本匹配
    hit2 = rules.match_rule(db_session, make_txn("Uber Trip"))
    assert hit2 is not None and hit2.id == rule.id


def test_priority_exact_over_contains_over_regex(db_session):
    regex_rule = add_rule(db_session, r"星巴克", RuleMatchType.REGEX, category="c-regex")
    contains_rule = add_rule(db_session, "星巴克", RuleMatchType.CONTAINS, category="c-contains")
    exact_rule = add_rule(db_session, "星巴克", RuleMatchType.EXACT, category="c-exact")
    txn = make_txn("星巴克")

    hit = rules.match_rule(db_session, txn)
    assert hit is not None and hit.id == exact_rule.id

    # 禁用 exact 后应落到 contains
    exact_rule.enabled = False
    db_session.flush()
    hit = rules.match_rule(db_session, txn)
    assert hit is not None and hit.id == contains_rule.id

    # 再禁用 contains 后落到 regex
    contains_rule.enabled = False
    db_session.flush()
    hit = rules.match_rule(db_session, txn)
    assert hit is not None and hit.id == regex_rule.id


def test_disabled_rule_not_matched(db_session):
    add_rule(db_session, "肯德基", RuleMatchType.EXACT, enabled=False)
    assert rules.match_rule(db_session, make_txn("肯德基")) is None


def test_hit_count_increments_per_match(db_session):
    rule = add_rule(db_session, "瑞幸", RuleMatchType.EXACT)
    rules.match_rule(db_session, make_txn("瑞幸"))
    rules.match_rule(db_session, make_txn("瑞幸"))
    assert rule.hit_count == 2


def test_no_match_returns_none(db_session):
    add_rule(db_session, "星巴克", RuleMatchType.EXACT)
    assert rules.match_rule(db_session, make_txn("完全无关商户")) is None


def test_learn_rule_creates_new(db_session):
    rule = rules.learn_rule(db_session, " KFC ", "餐饮")
    assert rule.id is not None
    assert rule.merchant_pattern == "kfc"  # 存储归一化后的 merchant
    assert rule.match_type == RuleMatchType.EXACT
    assert rule.category == "餐饮"
    assert rule.origin == RuleOrigin.CORRECTION
    assert db_session.scalars(select(Rule)).all() == [rule]
    # 学到的规则能被 exact 命中
    assert rules.match_rule(db_session, make_txn("kfc")) is rule


def test_learn_rule_updates_existing(db_session):
    first = rules.learn_rule(db_session, "肯德基", "餐饮")
    second = rules.learn_rule(db_session, "肯德基", "快餐")
    assert second.id == first.id
    assert second.category == "快餐"
    assert len(db_session.scalars(select(Rule)).all()) == 1


def test_invalid_regex_skipped_without_crash(db_session):
    add_rule(db_session, "([", RuleMatchType.REGEX)
    # 非法正则单独存在时不炸、不命中
    assert rules.match_rule(db_session, make_txn("任意商户")) is None
    # 且不影响其后合法规则的匹配
    valid = add_rule(db_session, r"滴滴", RuleMatchType.REGEX, category="出行")
    hit = rules.match_rule(db_session, make_txn("滴滴出行"))
    assert hit is not None and hit.id == valid.id


def test_record_audit_persists(db_session):
    rules.record_audit(db_session, "trace123", "ingest.received", {"amount": "25.00"})
    rows = db_session.scalars(select(AuditLog)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.trace_id == "trace123"
    assert row.event == "ingest.received"
    assert row.payload == {"amount": "25.00"}


def test_record_audit_allows_null_payload(db_session):
    rules.record_audit(db_session, "trace456", "ingest.done")
    row = db_session.scalars(select(AuditLog)).one()
    assert row.payload is None
