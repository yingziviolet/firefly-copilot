"""哨兵重复扣费检测单测:假 Firefly 客户端 + 记录通知调用,不触真实网络。"""

from typing import Any

import pytest

from app.services.firefly_client import FireflyError
from app.worker import tasks_sentinel


class FakeFirefly:
    """假客户端:记录 list_transactions 调用参数,按配置返回 split 列表或抛异常。"""

    def __init__(
        self, splits: list[dict[str, Any]] | None = None, exc: Exception | None = None
    ) -> None:
        self.splits = splits or []
        self.exc = exc
        self.calls: list[tuple[Any, Any, str]] = []

    def list_transactions(self, start, end, txn_type: str = "withdrawal"):
        self.calls.append((start, end, txn_type))
        if self.exc is not None:
            raise self.exc
        return self.splits


def _split(dest: str, amount: str, dt: str, journal: str) -> dict[str, Any]:
    return {
        "description": "订阅扣费",
        "amount": amount,
        "date": dt,
        "destination_name": dest,
        "transaction_journal_id": journal,
    }


@pytest.fixture()
def sent(monkeypatch) -> list[dict[str, Any]]:
    """替换 notify,记录每次调用。"""
    calls: list[dict[str, Any]] = []

    def fake_notify(text: str, parse_mode: str | None = None) -> bool:
        calls.append({"text": text, "parse_mode": parse_mode})
        return True

    monkeypatch.setattr(tasks_sentinel, "notify", fake_notify)
    return calls


def _use_fake_firefly(monkeypatch, fake: FakeFirefly) -> None:
    monkeypatch.setattr(tasks_sentinel, "get_firefly_client", lambda: fake)


def test_duplicate_group_alerts_once_with_details(monkeypatch, sent):
    # 商户名带空白/金额 25.00 vs 25.0,归一化后应归入同组
    fake = FakeFirefly(
        splits=[
            _split(" 爱奇艺 ", "25.00", "2026-07-24T08:00:00+08:00", "1"),
            _split("爱奇艺", "25.0", "2026-07-25T08:00:00+08:00", "2"),
            _split("肯德基", "36.50", "2026-07-25T12:00:00+08:00", "3"),
        ]
    )
    _use_fake_firefly(monkeypatch, fake)

    result = tasks_sentinel.scan_duplicate_charges.run(days=3)

    assert result == {"groups": 1, "checked": 3}
    assert len(sent) == 1
    text = sent[0]["text"]
    # 告警文本含商户、金额、各笔日期
    assert "爱奇艺" in text
    assert "25.00" in text
    assert "2026-07-24" in text
    assert "2026-07-25" in text
    # 未命中的商户不应出现在告警里
    assert "肯德基" not in text
    # 扫描窗口按 days 计算,类型固定 withdrawal
    start, end, txn_type = fake.calls[0]
    assert (end - start).days == 3
    assert txn_type == "withdrawal"


def test_no_duplicates_no_alert(monkeypatch, sent):
    # 同商户不同金额 / 同金额不同商户,均不构成重复
    fake = FakeFirefly(
        splits=[
            _split("肯德基", "36.50", "2026-07-25T12:00:00+08:00", "1"),
            _split("麦当劳", "36.50", "2026-07-25T13:00:00+08:00", "2"),
            _split("肯德基", "20.00", "2026-07-24T12:00:00+08:00", "3"),
        ]
    )
    _use_fake_firefly(monkeypatch, fake)

    result = tasks_sentinel.scan_duplicate_charges.run()

    assert result == {"groups": 0, "checked": 3}
    assert sent == []


def test_firefly_error_returns_error_dict(monkeypatch, sent):
    fake = FakeFirefly(exc=FireflyError("Firefly API GET failed: HTTP 500, body='boom'"))
    _use_fake_firefly(monkeypatch, fake)

    result = tasks_sentinel.scan_duplicate_charges.run()

    assert set(result) == {"error"}
    assert "boom" in result["error"]
    assert sent == []
