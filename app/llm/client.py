"""LLM 客户端:官方 anthropic SDK,base_url 可配置(三期切自研网关只改这一项)。

实现要求:
- anthropic.Anthropic(api_key=settings.anthropic_api_key, base_url=settings.anthropic_base_url
  or 默认, timeout=settings.llm_timeout)
- 分类用 client.messages.parse(..., output_format=LLMClassification) 结构化输出强校验
- output_config={"effort": settings.llm_effort}
- 校验失败/异常向上抛 LLMError,由 classifier 兜底
- LLM 返回的 category 不在候选列表内时,视为校验失败重试一次,仍失败抛 LLMError
"""

from datetime import date
from functools import lru_cache
from typing import Any

import anthropic

from app.config import get_settings
from app.logger import get_logger
from app.schemas.classify import DEFAULT_CATEGORIES, LLMClassification
from app.schemas.finance import FinanceQuery, RawFinanceIntent
from app.schemas.transaction import CanonicalTransaction, TxnDirection

logger = get_logger("app.llm.client")

# 方向枚举 -> 提示词里的中文标签
_DIRECTION_LABELS = {
    TxnDirection.EXPENSE: "支出",
    TxnDirection.INCOME: "收入",
    TxnDirection.TRANSFER: "转账",
}


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, client: "anthropic.Anthropic | None" = None) -> None:
        self._settings = get_settings()
        if client is None:
            kwargs: dict[str, Any] = {
                "api_key": self._settings.anthropic_api_key,
                "timeout": self._settings.llm_timeout,
            }
            # base_url 为 None 时不传,走 SDK 默认;配置后即切到自研网关
            if self._settings.anthropic_base_url is not None:
                kwargs["base_url"] = self._settings.anthropic_base_url
            client = anthropic.Anthropic(**kwargs)
        self._client = client

    def classify_transaction(
        self, txn: CanonicalTransaction, categories: list[str]
    ) -> LLMClassification:
        system = self._build_system_prompt(categories)
        summary = self._build_txn_summary(txn)

        result = self._parse_once(system, summary)
        if result.category in categories:
            return result

        # 越界分类:附纠正提示重试一次
        logger.warning(
            "llm_category_out_of_range",
            category=result.category,
            counterparty=txn.counterparty,
        )
        retry_summary = (
            f"{summary}\n\n"
            f"注意:你上次返回的分类「{result.category}」不在候选分类列表中,"
            "请严格从候选分类列表中重新选择一个分类。"
        )
        result = self._parse_once(system, retry_summary)
        if result.category not in categories:
            raise LLMError(f"LLM 分类 {result.category!r} 不在候选列表中(重试后仍失败)")
        return result

    def parse_finance_query(self, question: str, today: date) -> FinanceQuery:
        categories = "、".join(DEFAULT_CATEGORIES)
        system = (
            f"你是记账查询意图解析器。今天是 {today.isoformat()}。"
            f"可用分类：{categories}。"
            "只提取时间、收入或支出、分类、商户、金额合计或笔数。"
            "日期必须输出 YYYY-MM-DD；没有时间时使用本月至今。"
            "这两个月表示上一个自然月1日至今天，最近两个月表示滚动两个月。"
            "分类优先从可用分类中选择，具体店名或平台放入商户。"
            "用户只说商户时 category 必须为 null，不要推断分类。"
            "只返回 JSON 对象，不要输出解释、Markdown 或计算结果。"
            "不得生成 SQL、URL、代码或理财建议。"
        )
        last_error: Exception | None = None
        for attempt in range(2):
            content = question
            if last_error is not None:
                content += f"\n上次参数无效：{last_error}\n请修正后重新提取。"
            try:
                response = self._client.messages.parse(
                    model=self._settings.llm_model,
                    max_tokens=self._settings.llm_max_tokens,
                    temperature=0,
                    thinking={"type": "disabled"},
                    system=system,
                    messages=[{"role": "user", "content": content}],
                    output_format=RawFinanceIntent,
                )
                parsed = getattr(response, "parsed_output", None)
                if parsed is None:
                    raise ValueError("模型未返回查账意图")
                return parsed.to_query(question, today)
            except Exception as exc:
                last_error = exc
                logger.warning("finance_query_intent_invalid", attempt=attempt + 1, error=str(exc))
        raise LLMError(f"LLM 查账意图解析失败: {last_error}") from last_error

    def _parse_once(self, system: str, user_content: str) -> LLMClassification:
        """单次结构化分类调用;任何 SDK/校验异常统一包装为 LLMError。"""
        try:
            response = self._client.messages.parse(
                model=self._settings.llm_model,
                max_tokens=self._settings.llm_max_tokens,
                output_config={"effort": self._settings.llm_effort},
                system=system,
                messages=[{"role": "user", "content": user_content}],
                output_format=LLMClassification,
            )
        except Exception as exc:
            raise LLMError(f"LLM 调用失败: {exc}") from exc

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise LLMError("LLM 未返回结构化输出")
        return parsed

    @staticmethod
    def _build_system_prompt(categories: list[str]) -> str:
        lines = "\n".join(f"- {c}" for c in categories)
        return (
            "你是记账分类助手。根据用户提供的交易信息,判断这笔交易最合适的分类。\n"
            "候选分类列表:\n"
            f"{lines}\n"
            "只能从列表中选,不得创造列表之外的分类。"
            "同时给出 0-1 的置信度 confidence 和一句话理由 rationale。"
        )

    @staticmethod
    def _build_txn_summary(txn: CanonicalTransaction) -> str:
        direction = _DIRECTION_LABELS.get(txn.direction, str(txn.direction))
        parts = [
            f"商户/对方: {txn.counterparty}",
            f"描述: {txn.description or '无'}",
            f"金额: {txn.amount} {txn.currency}",
            f"方向: {direction}",
        ]
        if txn.category_hint:
            parts.append(f"渠道分类提示: {txn.category_hint}")
        return "\n".join(parts)


@lru_cache
def get_llm_client() -> LLMClient:
    return LLMClient()
