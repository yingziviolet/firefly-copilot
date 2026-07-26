"""classifier 三级顺序单测:规则命中 -> LLM -> hint/其他 兜底。"""

from datetime import datetime
from decimal import Decimal

from app.config import get_settings
from app.llm.client import LLMError
from app.models.rule import Rule, RuleMatchType
from app.schemas.classify import DEFAULT_CATEGORIES, LLMClassification
from app.schemas.transaction import CanonicalTransaction, TxnDirection, TxnSource
from app.services import classifier


def make_txn(counterparty: str, category_hint: str | None = None) -> CanonicalTransaction:
    return CanonicalTransaction(
        source=TxnSource.MANUAL,
        direction=TxnDirection.EXPENSE,
        occurred_at=datetime(2026, 7, 1, 12, 0, 0),
        amount=Decimal("25.00"),
        counterparty=counterparty,
        category_hint=category_hint,
    )


class FakeLLMClient:
    """假 LLM 客户端:可预设返回值或异常,并记录调用参数。"""

    def __init__(
        self,
        result: LLMClassification | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[CanonicalTransaction, list[str]]] = []

    def classify_transaction(
        self, txn: CanonicalTransaction, categories: list[str]
    ) -> LLMClassification:
        self.calls.append((txn, list(categories)))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def install_fake(monkeypatch, fake: FakeLLMClient) -> None:
    monkeypatch.setattr(classifier, "get_llm_client", lambda: fake)


def add_rule(session, pattern: str, category: str, budget: str | None = None) -> Rule:
    rule = Rule(
        merchant_pattern=pattern,
        match_type=RuleMatchType.EXACT,
        category=category,
        budget=budget,
    )
    session.add(rule)
    session.flush()
    return rule


# ---------- 第一级:规则命中 ----------


def test_rule_hit_returns_rule_source_and_skips_llm(db_session, monkeypatch):
    rule = add_rule(db_session, "星巴克", "餐饮", budget="吃喝预算")
    fake = FakeLLMClient(error=AssertionError("规则命中时不应调用 LLM"))
    install_fake(monkeypatch, fake)

    result = classifier.classify(db_session, make_txn("星巴克"))

    assert result.source == "rule"
    assert result.confidence == 1.0
    assert result.rule_id == rule.id
    assert result.category == "餐饮"
    assert result.budget == "吃喝预算"
    assert result.model is None
    assert fake.calls == []


# ---------- 第二级:LLM 路径 ----------


def test_llm_path_fills_model_and_rationale(db_session, monkeypatch):
    fake = FakeLLMClient(
        result=LLMClassification(category="交通", confidence=0.85, rationale="打车服务")
    )
    install_fake(monkeypatch, fake)

    result = classifier.classify(db_session, make_txn("某网约车平台"))

    assert result.source == "llm"
    assert result.category == "交通"
    assert result.confidence == 0.85  # 透传 LLM 置信度
    assert result.model == get_settings().llm_model
    assert result.rationale == "打车服务"
    assert result.rule_id is None


def test_llm_receives_default_categories_when_none(db_session, monkeypatch):
    fake = FakeLLMClient(
        result=LLMClassification(category="其他", confidence=0.5, rationale="无法判断")
    )
    install_fake(monkeypatch, fake)

    classifier.classify(db_session, make_txn("未知商户"), categories=None)

    assert len(fake.calls) == 1
    assert fake.calls[0][1] == DEFAULT_CATEGORIES


def test_llm_receives_custom_categories(db_session, monkeypatch):
    fake = FakeLLMClient(
        result=LLMClassification(category="咖啡", confidence=0.9, rationale="咖啡店")
    )
    install_fake(monkeypatch, fake)

    custom = ["咖啡", "其他"]
    result = classifier.classify(db_session, make_txn("独立咖啡馆"), categories=custom)

    assert fake.calls[0][1] == custom
    assert result.category == "咖啡"


# ---------- 第三级:兜底 ----------


def test_llm_error_with_hint_falls_back_to_hint(db_session, monkeypatch):
    fake = FakeLLMClient(error=LLMError("网络超时"))
    install_fake(monkeypatch, fake)

    result = classifier.classify(
        db_session, make_txn("神秘商户", category_hint="数码电器")
    )

    assert result.source == "hint"
    assert result.confidence == 0.3
    assert result.category == "数码电器"
    assert result.model is None
    assert result.rule_id is None


def test_llm_error_without_hint_falls_back_to_other(db_session, monkeypatch):
    fake = FakeLLMClient(error=LLMError("校验失败"))
    install_fake(monkeypatch, fake)

    result = classifier.classify(db_session, make_txn("神秘商户"))

    assert result.category == "其他"
    assert result.confidence == 0.0
    # schema 的 source 只有 rule/llm/hint 三值,无 hint 的兜底同属第三级,取 "hint"
    assert result.source == "hint"


def test_rule_priority_over_llm_even_if_llm_available(db_session, monkeypatch):
    add_rule(db_session, "美团", "外卖")
    fake = FakeLLMClient(
        result=LLMClassification(category="购物", confidence=0.99, rationale="不应被使用")
    )
    install_fake(monkeypatch, fake)

    result = classifier.classify(db_session, make_txn("美团"))

    assert result.source == "rule"
    assert result.category == "外卖"
    assert fake.calls == []
