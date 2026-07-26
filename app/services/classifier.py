"""分类器:规则优先 -> LLM 兜底 -> 置信度门控由调用方(ingest 管道)执行。

注意:LLM 结果不直接建规则,只有人工复核确认/改正才回流规则库。
"""

from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm.client import LLMError, get_llm_client
from app.logger import get_logger
from app.schemas.classify import DEFAULT_CATEGORIES, ClassificationResult
from app.schemas.transaction import CanonicalTransaction
from app.services.rules import match_rule

logger = get_logger(__name__)

# 三级兜底:hint 的固定置信度;无 hint 时的保底分类(置信度 0 必进人工复核)
_HINT_CONFIDENCE = 0.3
_FALLBACK_CATEGORY = "其他"


def classify(
    session: Session,
    txn: CanonicalTransaction,
    categories: list[str] | None = None,
) -> ClassificationResult:
    """顺序:
    1. rules.match_rule 命中 -> source="rule", confidence=1.0
    2. 未命中 -> llm.client 分类 -> source="llm",透传 LLM 置信度
    3. LLM 失败(网络/校验) -> 若有 category_hint 用之(source="hint", confidence=0.3),
       否则 category="其他", confidence=0.0(必然进人工复核)
    categories 为 None 时使用 DEFAULT_CATEGORIES。
    """
    candidates = DEFAULT_CATEGORIES if categories is None else categories

    # 第一级:规则命中,置信度恒为 1.0,免 LLM 成本
    rule = match_rule(session, txn)
    if rule is not None:
        return ClassificationResult(
            category=rule.category,
            budget=rule.budget,
            confidence=1.0,
            source="rule",
            rule_id=rule.id,
        )

    # 第二级:LLM 分类,透传置信度与理由
    try:
        llm_result = get_llm_client().classify_transaction(txn, candidates)
    except LLMError as exc:
        return _fallback(txn, exc)

    return ClassificationResult(
        category=llm_result.category,
        confidence=llm_result.confidence,
        source="llm",
        model=get_settings().llm_model,
        rationale=llm_result.rationale,
    )


def _fallback(txn: CanonicalTransaction, exc: LLMError) -> ClassificationResult:
    """第三级:LLM 失败后的兜底(有渠道提示用提示,否则归"其他")。"""
    logger.warning(
        "llm_classify_failed",
        error=str(exc),
        counterparty=txn.counterparty,
        has_hint=bool(txn.category_hint),
    )
    if txn.category_hint:
        return ClassificationResult(
            category=txn.category_hint,
            confidence=_HINT_CONFIDENCE,
            source="hint",
        )
    return ClassificationResult(
        category=_FALLBACK_CATEGORY,
        confidence=0.0,
        source="hint",
    )
