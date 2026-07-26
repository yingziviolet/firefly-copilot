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


class WeeklyFirefly:
    def __init__(self, withdrawals, deposits):
        self.withdrawals = withdrawals
        self.deposits = deposits
        self.calls = []

    def list_transactions(self, start, end, txn_type: str = "withdrawal"):
        self.calls.append((start, end, txn_type))
        return self.deposits if txn_type == "deposit" else self.withdrawals


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


def test_multiple_duplicate_groups_are_sent_as_one_message(monkeypatch, sent):
    fake = FakeFirefly(
        splits=[
            _split("爱奇艺", "25", "2026-07-24", "1"),
            _split("爱奇艺", "25", "2026-07-25", "2"),
            _split("网约车", "18", "2026-07-24", "3"),
            _split("网约车", "18", "2026-07-25", "4"),
        ]
    )
    _use_fake_firefly(monkeypatch, fake)

    result = tasks_sentinel.scan_duplicate_charges.run()

    assert result == {"groups": 2, "checked": 4}
    assert len(sent) == 1
    assert "爱奇艺" in sent[0]["text"]
    assert "网约车" in sent[0]["text"]


def test_firefly_error_returns_error_dict(monkeypatch, sent):
    fake = FakeFirefly(exc=FireflyError("Firefly API GET failed: HTTP 500, body='boom'"))
    _use_fake_firefly(monkeypatch, fake)

    result = tasks_sentinel.scan_duplicate_charges.run()

    assert set(result) == {"error"}
    assert "boom" in result["error"]
    assert sent == []


def test_weekly_digest_sends_one_complete_message(monkeypatch, sent):
    withdrawals = [
        {**_split("美团外卖", "50", "2026-07-20", "1"), "category_name": "餐饮"},
        {**_split("便利店", "20", "2026-07-21", "2"), "category_name": "日用"},
        {**_split("便利店", "20", "2026-07-22", "3"), "category_name": "日用"},
        _split("视频会员", "25", "2026-05-27", "4"),
        _split("视频会员", "25", "2026-06-26", "5"),
        _split("视频会员", "30", "2026-07-26", "6"),
    ]
    deposits = [
        {
            "source_name": "工资",
            "amount": "5000",
            "date": "2026-07-25",
            "category_name": "工资",
        }
    ]
    fake = WeeklyFirefly(withdrawals, deposits)
    _use_fake_firefly(monkeypatch, fake)

    result = tasks_sentinel.send_weekly_digest.run(today_iso="2026-07-27")

    assert result == {"withdrawals": 4, "deposits": 1, "subscriptions": 1, "duplicates": 1}
    assert len(sent) == 1
    text = sent[0]["text"]
    assert "2026-07-20 至 2026-07-26" in text
    assert "总收入:5000.00" in text
    assert "总支出:120.00" in text
    assert "餐饮:50.00" in text
    assert "视频会员" in text
    assert "25.00 → 30.00" in text
    assert "便利店" in text


def test_weekly_digest_firefly_error_does_not_notify(monkeypatch, sent):
    _use_fake_firefly(monkeypatch, FakeFirefly(exc=FireflyError("boom")))

    result = tasks_sentinel.send_weekly_digest.run(today_iso="2026-07-27")

    assert result == {"error": "boom"}
    assert sent == []
