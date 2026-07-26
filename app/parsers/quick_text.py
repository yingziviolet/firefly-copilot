"""快捷记账文本解析:「早餐 15」「昨天 打车 23.5」-> CanonicalTransaction。

供 Web 控制台快捷记账使用的纯函数,规则:
- 取文本中最后一个数字为金额,其余为描述/商户
- 支持「昨天/前天」相对日期词
- 无金额返回 None
"""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from app.schemas.transaction import CanonicalTransaction, TxnDirection, TxnSource

# 金额:整数或小数
_AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?")
# 相对日期词 -> 回溯天数
_DATE_WORDS: dict[str, int] = {"前天": 2, "昨天": 1}
# 金额附带的货币字样,解析后从描述中剔除
_CURRENCY_RE = re.compile(r"[¥￥]|块钱|[元块]")


def parse_quick_expense(text: str) -> CanonicalTransaction | None:
    """解析快捷记账文本:最后一个数字为金额,其余为描述/商户;无金额返回 None。"""
    text = (text or "").strip()
    if not text:
        return None

    matches = list(_AMOUNT_RE.finditer(text))
    if not matches:
        return None
    last = matches[-1]
    try:
        amount = Decimal(last.group())
    except InvalidOperation:  # pragma: no cover - 正则已保证格式
        return None
    if amount <= 0:
        return None

    # 去掉金额后剩下的部分为描述
    remainder = text[: last.start()] + text[last.end() :]

    # 相对日期词:昨天/前天
    day_offset = 0
    for word, offset in _DATE_WORDS.items():
        if word in remainder:
            remainder = remainder.replace(word, " ")
            day_offset = offset
            break

    remainder = _CURRENCY_RE.sub(" ", remainder)
    description = " ".join(remainder.split())
    counterparty = description or "未知"

    return CanonicalTransaction(
        source=TxnSource.MANUAL,
        direction=TxnDirection.EXPENSE,
        occurred_at=datetime.now(UTC) - timedelta(days=day_offset),
        amount=amount,
        counterparty=counterparty,
        description=description,
        raw={"text": text},
    )
