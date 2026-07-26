"""Bot 单测:parse_quick_expense 纯解析、白名单、callback 数据格式、handler 驱动。"""

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.bot import handlers, runner
from app.models.review import ReviewStatus
from app.schemas.classify import DEFAULT_CATEGORIES, ClassificationResult
from app.schemas.transaction import CanonicalTransaction, TxnDirection, TxnSource
from app.services import review

# ---------------------------------------------------------------------------
# parse_quick_expense
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, amount, counterparty",
    [
        ("早餐 15", Decimal("15"), "早餐"),
        ("打车 23.5", Decimal("23.5"), "打车"),
        ("奶茶15元", Decimal("15"), "奶茶"),
        ("地铁2号线 5", Decimal("5"), "地铁2号线"),
    ],
)
def test_parse_quick_expense_basic(text, amount, counterparty):
    txn = handlers.parse_quick_expense(text)
    assert txn is not None
    assert txn.source == TxnSource.TELEGRAM
    assert txn.direction == TxnDirection.EXPENSE
    assert txn.amount == amount
    assert txn.counterparty == counterparty
    assert txn.raw == {"text": text}


def test_parse_quick_expense_today_by_default():
    txn = handlers.parse_quick_expense("早餐 15")
    assert txn is not None
    assert abs(datetime.now(UTC) - txn.occurred_at) < timedelta(minutes=1)


@pytest.mark.parametrize(
    "text, days_ago, counterparty",
    [
        ("昨天 午饭 20", 1, "午饭"),
        ("前天 咖啡 30", 2, "咖啡"),
        ("午饭 20 昨天", 1, "午饭"),
    ],
)
def test_parse_quick_expense_relative_date(text, days_ago, counterparty):
    txn = handlers.parse_quick_expense(text)
    assert txn is not None
    expected = datetime.now(UTC) - timedelta(days=days_ago)
    assert abs(expected - txn.occurred_at) < timedelta(minutes=1)
    assert txn.counterparty == counterparty
    assert txn.amount == Decimal("20") or txn.amount == Decimal("30")


@pytest.mark.parametrize("text", ["没有数字", "", "   ", "/start", "0"])
def test_parse_quick_expense_unparsable(text):
    assert handlers.parse_quick_expense(text) is None


def test_parse_quick_expense_amount_only_falls_back_counterparty():
    txn = handlers.parse_quick_expense("15")
    assert txn is not None
    assert txn.amount == Decimal("15")
    assert txn.counterparty == "未知"


# ---------------------------------------------------------------------------
# 白名单
# ---------------------------------------------------------------------------


def test_is_allowed_user():
    # conftest: TELEGRAM_ALLOWED_USER_IDS=10001
    assert handlers.is_allowed_user(10001) is True
    assert handlers.is_allowed_user(999) is False
    assert handlers.is_allowed_user(None) is False


# ---------------------------------------------------------------------------
# callback data 构造/解析
# ---------------------------------------------------------------------------


def test_build_cb_formats():
    assert handlers.build_cb("approve", 3) == "approve:3"
    assert handlers.build_cb("correct", 12) == "correct:12"
    assert handlers.build_cb("reject", 7) == "reject:7"
    assert handlers.build_cb("correctcat", 7, "餐饮") == "correctcat:7:餐饮"


def test_parse_cb_roundtrip():
    assert handlers.parse_cb("approve:3") == ("approve", 3, None)
    assert handlers.parse_cb("correctcat:7:餐饮") == ("correctcat", 7, "餐饮")
    for data in ("approve:3", "reject:99", "correctcat:5:人情往来"):
        action, item_id, category = handlers.parse_cb(data)
        assert handlers.build_cb(action, item_id, category) == data


@pytest.mark.parametrize("data", ["nodata", "approve:abc", ":3"])
def test_parse_cb_invalid_raises(data):
    with pytest.raises(ValueError):
        handlers.parse_cb(data)


def test_pending_keyboard_callback_data():
    kb = handlers.build_pending_keyboard(42)
    datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert datas == ["approve:42", "correct:42", "reject:42"]


def test_category_keyboard_covers_default_categories():
    kb = handlers.build_category_keyboard(9)
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    assert [btn.text for btn in buttons] == DEFAULT_CATEGORIES
    for btn in buttons:
        action, item_id, category = handlers.parse_cb(btn.callback_data)
        assert action == "correctcat"
        assert item_id == 9
        assert category == btn.text


# ---------------------------------------------------------------------------
# handler 驱动(假 Message / CallbackQuery)
# ---------------------------------------------------------------------------


class FakeUser:
    def __init__(self, user_id: int):
        self.id = user_id


class FakeMessage:
    def __init__(self, text: str, user_id: int = 10001):
        self.text = text
        self.from_user = FakeUser(user_id)
        self.answers: list[tuple[str, object]] = []
        self.edits: list[tuple[str, object]] = []

    async def answer(self, text: str, reply_markup=None):
        self.answers.append((text, reply_markup))

    async def edit_text(self, text: str, reply_markup=None):
        self.edits.append((text, reply_markup))


class FakeCallback:
    def __init__(self, data: str, user_id: int = 10001):
        self.data = data
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage("")
        self.answers: list[tuple[object, bool]] = []

    async def answer(self, text=None, show_alert: bool = False):
        self.answers.append((text, show_alert))


@pytest.fixture()
def delay_calls(monkeypatch):
    """monkeypatch 两个任务的 .delay,记录调用参数。"""
    calls: dict[str, list] = {"ingest": [], "finalize": []}
    monkeypatch.setattr(
        handlers.ingest_transaction, "delay", lambda *a, **k: calls["ingest"].append((a, k))
    )
    monkeypatch.setattr(
        handlers.finalize_review, "delay", lambda *a, **k: calls["finalize"].append((a, k))
    )
    return calls


@pytest.fixture()
def patched_scope(monkeypatch, db_session):
    """把 handlers.session_scope 换成直接吐测试会话(不 commit/close)。"""

    @contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(handlers, "session_scope", _scope)
    return db_session


def test_text_handler_enqueues_ingest(delay_calls):
    msg = FakeMessage("早餐 15")
    asyncio.run(handlers.handle_quick_expense(msg))

    assert len(delay_calls["ingest"]) == 1
    (args, kwargs) = delay_calls["ingest"][0]
    payload, trace_id = args
    assert kwargs == {}
    assert payload["counterparty"] == "早餐"
    assert payload["amount"] == "15"
    assert payload["source"] == "telegram"
    assert payload["direction"] == "withdrawal"
    assert isinstance(trace_id, str) and trace_id
    # 队列 payload 可反序列化为 CanonicalTransaction
    assert CanonicalTransaction.load_from_queue(payload).amount == Decimal("15")
    assert any("早餐" in text for text, _ in msg.answers)


def test_text_handler_rejects_non_whitelist(delay_calls):
    msg = FakeMessage("早餐 15", user_id=999)
    asyncio.run(handlers.handle_quick_expense(msg))
    assert delay_calls["ingest"] == []
    assert any("白名单" in text for text, _ in msg.answers)


def test_text_handler_unparsable_replies_usage(delay_calls):
    msg = FakeMessage("你好呀")
    asyncio.run(handlers.handle_quick_expense(msg))
    assert delay_calls["ingest"] == []
    assert any("记一笔账" in text for text, _ in msg.answers)


def _create_pending_item(session, counterparty: str = "星巴克"):
    txn = CanonicalTransaction(
        source=TxnSource.TELEGRAM,
        direction=TxnDirection.EXPENSE,
        occurred_at=datetime(2026, 7, 25, 12, 0),
        amount=Decimal("35.00"),
        counterparty=counterparty,
        description="咖啡",
    )
    suggestion = ClassificationResult(category="餐饮", confidence=0.5, source="llm")
    return review.create_review_item(
        session, txn, fingerprint=f"fp-{counterparty}", suggestion=suggestion, trace_id="t-1"
    )


def test_cb_approve_transitions_and_finalizes(patched_scope, delay_calls):
    item = _create_pending_item(patched_scope)
    cb = FakeCallback(handlers.build_cb("approve", item.id))
    asyncio.run(handlers.cb_approve(cb))

    assert item.status == ReviewStatus.APPROVED
    assert delay_calls["finalize"] == [((item.id,), {})]
    assert cb.message.edits and "已批准" in cb.message.edits[0][0]


def test_cb_correct_shows_category_keyboard(patched_scope, delay_calls):
    item = _create_pending_item(patched_scope)
    cb = FakeCallback(handlers.build_cb("correct", item.id))
    asyncio.run(handlers.cb_correct(cb))

    # 只展开分类按钮,不做状态流转、不 finalize
    assert item.status == ReviewStatus.PENDING
    assert delay_calls["finalize"] == []
    text, markup = cb.message.edits[0]
    datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert handlers.build_cb("correctcat", item.id, "餐饮") in datas


def test_cb_correctcat_corrects_and_finalizes(patched_scope, delay_calls):
    item = _create_pending_item(patched_scope)
    cb = FakeCallback(handlers.build_cb("correctcat", item.id, "购物"))
    asyncio.run(handlers.cb_correct_category(cb))

    assert item.status == ReviewStatus.CORRECTED
    assert item.corrected_category == "购物"
    assert delay_calls["finalize"] == [((item.id,), {})]


def test_cb_reject_no_finalize(patched_scope, delay_calls):
    item = _create_pending_item(patched_scope)
    cb = FakeCallback(handlers.build_cb("reject", item.id))
    asyncio.run(handlers.cb_reject(cb))

    assert item.status == ReviewStatus.REJECTED
    assert delay_calls["finalize"] == []


def test_cb_approve_missing_item_alerts(patched_scope, delay_calls):
    cb = FakeCallback(handlers.build_cb("approve", 9999))
    asyncio.run(handlers.cb_approve(cb))
    assert delay_calls["finalize"] == []
    assert cb.answers and cb.answers[0][1] is True  # show_alert


def test_cb_guard_rejects_non_whitelist(patched_scope, delay_calls):
    cb = FakeCallback(handlers.build_cb("approve", 1), user_id=999)
    asyncio.run(handlers.cb_approve(cb))
    assert delay_calls["finalize"] == []
    assert cb.answers == [("你不在使用白名单内。", True)]


def test_cmd_pending_lists_items(patched_scope):
    item = _create_pending_item(patched_scope)
    msg = FakeMessage("/pending")
    asyncio.run(handlers.cmd_pending(msg))

    assert len(msg.answers) == 1
    text, markup = msg.answers[0]
    assert f"#{item.id}" in text and "星巴克" in text
    datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert datas == [
        handlers.build_cb("approve", item.id),
        handlers.build_cb("correct", item.id),
        handlers.build_cb("reject", item.id),
    ]


def test_cmd_start_replies_usage():
    msg = FakeMessage("/start")
    asyncio.run(handlers.cmd_start(msg))
    assert any("记一笔账" in text for text, _ in msg.answers)


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


def test_runner_main_requires_token(monkeypatch):
    from app.config import Settings

    monkeypatch.setattr(runner, "get_settings", lambda: Settings(telegram_bot_token=""))
    with pytest.raises(RuntimeError):
        runner.main()


def test_runner_main_starts_polling(monkeypatch):
    seen: dict[str, object] = {}

    class FakeBot:
        def __init__(self, token: str):
            seen["token"] = token

    class FakeDispatcher:
        def __init__(self):
            self.routers: list = []
            seen["dp"] = self

        def include_router(self, r):
            self.routers.append(r)

        async def start_polling(self, bot):
            seen["polling_bot"] = bot

    def fake_run(coro):
        coro.close()  # 避免 "coroutine never awaited" 警告
        seen["ran"] = True

    monkeypatch.setattr(runner, "Bot", FakeBot)
    monkeypatch.setattr(runner, "Dispatcher", FakeDispatcher)
    monkeypatch.setattr(runner.asyncio, "run", fake_run)

    runner.main()

    assert seen["token"] == "123456:test-token"
    assert seen["dp"].routers == [handlers.router]
    assert seen["ran"] is True
