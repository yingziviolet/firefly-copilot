"""规则库:命中直接跳过 LLM(成本工程核心);用户改正自动回流。"""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logger import get_logger
from app.models.audit import AuditLog
from app.models.rule import Rule, RuleMatchType, RuleOrigin
from app.schemas.transaction import CanonicalTransaction

logger = get_logger(__name__)

# 匹配优先级:精确 > 包含 > 正则
_MATCH_PRIORITY = (RuleMatchType.EXACT, RuleMatchType.CONTAINS, RuleMatchType.REGEX)


def _normalize(text: str) -> str:
    """归一化:去首尾空白 + 小写。"""
    return text.strip().lower()


def _rule_hits(rule: Rule, target: str) -> bool:
    """单条规则是否命中归一化后的 counterparty;非法正则容错跳过。"""
    if rule.match_type == RuleMatchType.REGEX:
        try:
            return re.search(rule.merchant_pattern, target, re.IGNORECASE) is not None
        except re.error:
            logger.warning(
                "rule_regex_invalid", rule_id=rule.id, pattern=rule.merchant_pattern
            )
            return False
    pattern = _normalize(rule.merchant_pattern)
    if not pattern:
        return False
    if rule.match_type == RuleMatchType.EXACT:
        return pattern == target
    return pattern in target


def match_rule(session: Session, txn: CanonicalTransaction) -> Rule | None:
    """按优先级 exact > contains > regex 匹配 counterparty,只看 enabled 规则。

    命中后 hit_count += 1 并返回规则;未命中返回 None。
    contains/regex 匹配对象为归一化后的 counterparty(小写、去空白)。
    """
    target = _normalize(txn.counterparty)
    if not target:
        return None

    all_rules = session.scalars(
        select(Rule).where(Rule.enabled.is_(True)).order_by(Rule.id)
    ).all()
    by_type: dict[RuleMatchType, list[Rule]] = {}
    for rule in all_rules:
        by_type.setdefault(rule.match_type, []).append(rule)

    for match_type in _MATCH_PRIORITY:
        for rule in by_type.get(match_type, []):
            if _rule_hits(rule, target):
                rule.hit_count += 1
                session.flush()
                logger.info(
                    "rule_matched",
                    rule_id=rule.id,
                    match_type=rule.match_type.value,
                    category=rule.category,
                    counterparty=txn.counterparty,
                )
                return rule
    return None


def learn_rule(
    session: Session,
    merchant: str,
    category: str,
    origin: RuleOrigin = RuleOrigin.CORRECTION,
) -> Rule:
    """upsert:同 (merchant, exact) 规则存在则更新 category,否则新建。"""
    pattern = _normalize(merchant)
    rule = session.scalars(
        select(Rule).where(
            Rule.merchant_pattern == pattern,
            Rule.match_type == RuleMatchType.EXACT,
        )
    ).first()
    if rule is None:
        rule = Rule(
            merchant_pattern=pattern,
            match_type=RuleMatchType.EXACT,
            category=category,
            origin=origin,
        )
        session.add(rule)
        logger.info("rule_learned", merchant=pattern, category=category, origin=origin.value)
    else:
        rule.category = category
        logger.info("rule_updated", rule_id=rule.id, merchant=pattern, category=category)
    session.flush()
    return rule


def record_audit(session: Session, trace_id: str, event: str, payload: dict | None = None) -> None:
    """写一条审计日志(挂在这里避免循环依赖,各服务共用)。"""
    session.add(AuditLog(trace_id=trace_id, event=event, payload=payload))
    session.flush()
    # structlog 方法首个位置参数名为 event,避免同名 kwarg 冲突
    logger.debug("audit_recorded", trace_id=trace_id, audit_event=event)
