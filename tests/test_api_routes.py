"""upload / webhook 路由集成测试:真实解析器 + 真实验签,任务只 mock .delay。"""

import hashlib
import hmac
from unittest.mock import MagicMock

import pytest

from app.worker.tasks_ingest import handle_firefly_event, ingest_transaction

# ---------- 上传用内嵌小账单 ----------

ALIPAY_CSV = (
    "支付宝交易流水明细\n"
    "起始时间:[2026-07-01]  终止时间:[2026-07-05]\n"
    "---------------------------------交易记录明细列表------------------------------------\n"
    "交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注\n"
    "2026-07-01 12:00:00,餐饮美食,某某餐厅,,午餐,支出,25.50,余额宝,交易成功,20260701001,,\n"
    "2026-07-02 09:30:00,转账红包,张三,,红包,收入,100.00,余额,交易成功,20260702002,,\n"
    "2026-07-03 10:00:00,投资理财,余额宝,,收益发放,不计收支,0.35,余额宝,交易成功,20260703003,,\n"
    "2026-07-04 11:00:00,餐饮美食,某店,,退款单,支出,10.00,余额,退款成功,20260704004,,\n"
).encode()

WECHAT_CSV = (
    "微信支付账单明细\n"
    "导出时间:[2026-07-05 10:00:00]\n"
    "----------------------微信支付账单明细列表--------------------\n"
    "交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注\n"
    "2026-07-01 08:00:00,商户消费,便利店,饮料,支出,¥6.00,零钱,支付成功,wx1001,,\n"
    "2026-07-02 20:15:00,微信红包,李四,红包,收入,¥66.66,零钱,已存入零钱,wx1002,,\n"
    "2026-07-03 09:00:00,零钱提现,工商银行,提现,/,¥200.00,零钱,提现已到账,wx1003,,\n"
).encode()

# ---------- webhook 签名工具 ----------

SECRET = "test-secret"  # 与 conftest 设置的 FIREFLY_WEBHOOK_SECRET 一致
TS = "1753500000"


def sign(body: bytes, secret: str = SECRET, ts: str = TS) -> str:
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha3_256).hexdigest()
    return f"t={ts},v1={digest}"


@pytest.fixture()
def ingest_delay(monkeypatch):
    mock = MagicMock(name="ingest_transaction.delay")
    monkeypatch.setattr(ingest_transaction, "delay", mock)
    return mock


@pytest.fixture()
def webhook_delay(monkeypatch):
    mock = MagicMock(name="handle_firefly_event.delay")
    monkeypatch.setattr(handle_firefly_event, "delay", mock)
    return mock


def _upload(client, source: str, content: bytes):
    return client.post(
        "/api/upload/csv",
        params={"source": source},
        files={"file": ("bill.csv", content, "text/csv")},
    )


# ---------- upload:422 ----------


def test_upload_invalid_source_422(client, ingest_delay):
    resp = _upload(client, "bank", ALIPAY_CSV)
    assert resp.status_code == 422
    ingest_delay.assert_not_called()


def test_upload_missing_source_422(client, ingest_delay):
    resp = client.post("/api/upload/csv", files={"file": ("bill.csv", ALIPAY_CSV, "text/csv")})
    assert resp.status_code == 422
    ingest_delay.assert_not_called()


# ---------- upload:400 ----------


def test_upload_unparseable_csv_400(client, ingest_delay):
    resp = _upload(client, "alipay", b"hello,world\n1,2\n")
    assert resp.status_code == 400
    ingest_delay.assert_not_called()


def test_upload_bad_encoding_400(client, ingest_delay):
    # 0x80 既非合法 UTF-8 也非合法 GB18030 首字节
    resp = _upload(client, "wechat", b"\x80\x80\x80")
    assert resp.status_code == 400
    ingest_delay.assert_not_called()


# ---------- upload:202 ----------


def test_upload_alipay_202(client, ingest_delay):
    resp = _upload(client, "alipay", ALIPAY_CSV)
    assert resp.status_code == 202
    body = resp.json()
    assert body["enqueued"] == 2
    assert body["skipped"] == 2  # 不计收支 + 退款
    assert body["trace_id"]
    assert body["source"] == "alipay"

    assert ingest_delay.call_count == 2
    # 整批共用同一个 trace_id,且与响应一致
    trace_ids = {call.args[1] for call in ingest_delay.call_args_list}
    assert trace_ids == {body["trace_id"]}

    first = ingest_delay.call_args_list[0].args[0]
    assert first["source"] == "alipay"
    assert first["direction"] == "withdrawal"
    assert first["amount"] == "25.50"
    assert first["counterparty"] == "某某餐厅"
    assert first["source_ref"] == "20260701001"

    second = ingest_delay.call_args_list[1].args[0]
    assert second["direction"] == "deposit"
    assert second["amount"] == "100.00"


def test_upload_wechat_202(client, ingest_delay):
    resp = _upload(client, "wechat", WECHAT_CSV)
    assert resp.status_code == 202
    body = resp.json()
    assert body["enqueued"] == 2
    assert body["skipped"] == 1  # "/" 中性流水
    assert body["source"] == "wechat"

    assert ingest_delay.call_count == 2
    first = ingest_delay.call_args_list[0].args[0]
    assert first["source"] == "wechat"
    assert first["direction"] == "withdrawal"
    assert first["amount"] == "6.00"  # ¥ 前缀已清洗
    assert first["account_hint"] == "零钱"
    assert ingest_delay.call_args_list[0].args[1] == body["trace_id"]


# ---------- upload:source=auto ----------


def test_upload_auto_detects_alipay(client, ingest_delay):
    resp = _upload(client, "auto", ALIPAY_CSV)
    assert resp.status_code == 202
    body = resp.json()
    assert body["source"] == "alipay"
    assert body["enqueued"] == 2
    assert body["skipped"] == 2
    assert ingest_delay.call_count == 2
    assert ingest_delay.call_args_list[0].args[0]["source"] == "alipay"


def test_upload_auto_detects_wechat(client, ingest_delay):
    resp = _upload(client, "auto", WECHAT_CSV)
    assert resp.status_code == 202
    body = resp.json()
    assert body["source"] == "wechat"
    assert body["enqueued"] == 2
    assert body["skipped"] == 1
    assert ingest_delay.call_args_list[0].args[0]["source"] == "wechat"


def test_upload_auto_unrecognized_400(client, ingest_delay):
    # 无任何渠道特征:识别失败 -> 400
    resp = _upload(client, "auto", "交易时间,收/支,金额\n2026-07-01 12:00:00,支出,1.00\n".encode())
    assert resp.status_code == 400
    assert "无法自动识别账单渠道" in resp.json()["detail"]
    ingest_delay.assert_not_called()


# ---------- webhook ----------

WEBHOOK_BODY = b'{"trigger":"STORE_TRANSACTION","content":{"id":1}}'


def test_webhook_valid_signature_202(client, webhook_delay):
    resp = client.post(
        "/api/webhook/firefly",
        content=WEBHOOK_BODY,
        headers={"Signature": sign(WEBHOOK_BODY)},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert set(body) == {"trace_id"}

    webhook_delay.assert_called_once()
    payload, trace_id = webhook_delay.call_args.args
    assert payload == {"trigger": "STORE_TRANSACTION", "content": {"id": 1}}
    assert trace_id == body["trace_id"]


def test_webhook_invalid_signature_401(client, webhook_delay):
    resp = client.post(
        "/api/webhook/firefly",
        content=WEBHOOK_BODY,
        headers={"Signature": sign(WEBHOOK_BODY, secret="wrong-secret")},
    )
    assert resp.status_code == 401
    webhook_delay.assert_not_called()


def test_webhook_missing_signature_401(client, webhook_delay):
    resp = client.post("/api/webhook/firefly", content=WEBHOOK_BODY)
    assert resp.status_code == 401
    webhook_delay.assert_not_called()


def test_webhook_tampered_body_401(client, webhook_delay):
    resp = client.post(
        "/api/webhook/firefly",
        content=WEBHOOK_BODY + b"x",
        headers={"Signature": sign(WEBHOOK_BODY)},
    )
    assert resp.status_code == 401
    webhook_delay.assert_not_called()


def test_webhook_non_json_body_empty_payload(client, webhook_delay):
    raw = b"not-json-at-all"
    resp = client.post(
        "/api/webhook/firefly",
        content=raw,
        headers={"Signature": sign(raw)},
    )
    assert resp.status_code == 202
    payload, trace_id = webhook_delay.call_args.args
    assert payload == {}
    assert trace_id == resp.json()["trace_id"]
