"""交易指纹:幂等去重的基石。

规则(实现时严格遵守):
- 有 source_ref(渠道原始单号,全局唯一)时:sha256("{source}:{source_ref}")
- 无 source_ref 时:sha256("{date:YYYY-MM-DD}|{amount 两位小数}|{counterparty 归一化}|{direction}")
  counterparty 归一化 = 去首尾空白、全角转半角、小写
- 返回 64 位十六进制字符串
"""

import hashlib
import unicodedata
from decimal import ROUND_HALF_UP, Decimal

from app.schemas.transaction import CanonicalTransaction

# 两位小数的量化模板
_CENT = Decimal("0.01")


def _normalize_counterparty(value: str) -> str:
    # 先 NFKC 全角转半角(全角空格也会变半角),再去首尾空白、小写
    return unicodedata.normalize("NFKC", value).strip().lower()


def compute_fingerprint(txn: CanonicalTransaction) -> str:
    if txn.source_ref:
        # 渠道原始单号全局唯一,优先作为指纹来源;空字符串视同缺失
        payload = f"{txn.source.value}:{txn.source_ref}"
    else:
        amount = txn.amount.quantize(_CENT, rounding=ROUND_HALF_UP)
        payload = "|".join(
            (
                txn.occurred_at.date().isoformat(),
                str(amount),
                _normalize_counterparty(txn.counterparty),
                txn.direction.value,
            )
        )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
