"""分类结果契约:规则命中或 LLM 输出统一为 ClassificationResult。"""

from typing import Literal

from pydantic import BaseModel, Field

# Firefly 分类不可用时的兜底分类集;分类器优先用 Firefly API 拉取的实际分类
DEFAULT_CATEGORIES = [
    "餐饮",
    "交通",
    "购物",
    "日用",
    "居住",
    "娱乐",
    "医疗",
    "教育",
    "通讯",
    "人情往来",
    "订阅服务",
    "工资收入",
    "转账",
    "其他",
]


class LLMClassification(BaseModel):
    """LLM 结构化输出 schema(structured outputs 强校验)。"""

    category: str = Field(description="从给定分类列表中选择的分类")
    confidence: float = Field(ge=0, le=1, description="0-1 置信度")
    rationale: str = Field(description="一句话理由")


class ClassificationResult(BaseModel):
    category: str
    budget: str | None = None
    confidence: float = Field(ge=0, le=1)
    source: Literal["rule", "llm", "hint"]
    rule_id: int | None = None
    model: str | None = None
    rationale: str | None = None
