"""verify_signature 单测:自行构造 HMAC-SHA3-256 签名,不依赖网络。"""

import hashlib
import hmac

import pytest

from app.services.firefly_webhook import verify_signature

SECRET = "test-secret"
BODY = b'{"trigger":"STORE_TRANSACTION","content":{"id":1}}'
TS = "1753500000"


def sign(secret: str, body: bytes, ts: str = TS) -> str:
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha3_256).hexdigest()
    return f"t={ts},v1={digest}"


def test_valid_signature_passes():
    assert verify_signature(SECRET, sign(SECRET, BODY), BODY) is True


def test_uppercase_hex_accepted():
    digest = hmac.new(SECRET.encode(), f"{TS}.".encode() + BODY, hashlib.sha3_256).hexdigest()
    assert verify_signature(SECRET, f"t={TS},v1={digest.upper()}", BODY) is True


def test_tampered_body_fails():
    assert verify_signature(SECRET, sign(SECRET, BODY), BODY + b"x") is False


def test_wrong_secret_fails():
    assert verify_signature("other-secret", sign(SECRET, BODY), BODY) is False


def test_wrong_timestamp_fails():
    # 时间戳参与摘要:签名用 ts A、头里改成 ts B 必须失败
    header = sign(SECRET, BODY, ts="1753500000").replace("t=1753500000", "t=1753500001")
    assert verify_signature(SECRET, header, BODY) is False


def test_missing_header_fails():
    assert verify_signature(SECRET, None, BODY) is False


def test_empty_header_fails():
    assert verify_signature(SECRET, "", BODY) is False


@pytest.mark.parametrize(
    "header",
    [
        "garbage",
        "t=1753500000",
        "v1=abcdef",
        "t=1753500000;v1=abcdef",
        "t=,v1=",
        "t=1753500000,v1=",
    ],
)
def test_malformed_header_fails(header: str):
    assert verify_signature(SECRET, header, BODY) is False


def test_empty_secret_fails():
    # secret 未配置时即使签名"匹配"也拒绝,防止误放行
    assert verify_signature("", sign("", BODY), BODY) is False
