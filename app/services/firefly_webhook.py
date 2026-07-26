"""Firefly III webhook 签名校验。

签名机制(对齐官方文档 docs.firefly-iii.org 的 webhook 规范):
Signature 头格式形如 "t=<unix_ts>,v1=<hex>",摘要为 HMAC-SHA3-256(secret, f"{t}.{raw_body}")。
校验失败/头缺失一律返回 False;比较用 hmac.compare_digest。
"""

import hashlib
import hmac

from app.logger import get_logger

logger = get_logger(__name__)


def verify_signature(secret: str, signature_header: str | None, raw_body: bytes) -> bool:
    # secret 未配置或头缺失:直接判失败,避免误放行
    if not secret or not signature_header:
        return False
    fields: dict[str, str] = {}
    for part in signature_header.split(","):
        key, sep, value = part.partition("=")
        if not sep:
            # 出现非 key=value 片段即视为格式错误
            return False
        fields[key.strip()] = value.strip()
    timestamp = fields.get("t")
    provided = fields.get("v1")
    if not timestamp or not provided:
        return False
    payload = timestamp.encode("utf-8") + b"." + raw_body
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha3_256).hexdigest()
    valid = hmac.compare_digest(expected, provided.lower())
    if not valid:
        logger.warning("firefly_webhook_signature_mismatch")
    return valid
