"""Web 控制台集成测试:页面渲染、复核操作流转、快捷记账、CSV 上传、token 鉴权。

外部副作用全部 mock:finalize_review.delay / ingest_transaction.delay。
"""

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock
from urllib.parse import unquote_plus

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.review import ReviewItem, ReviewStatus
from app.models.rule import Rule, RuleMatchType
from app.schemas.classify import ClassificationResult
from app.schemas.finance import FinanceQuery
from app.schemas.transaction import CanonicalTransaction, TxnDirection, TxnSource
from app.services import review
from app.worker.tasks_ingest import finalize_review, ingest_transaction, reclassify_pending

# 与 test_api_routes 同构的小账单:2 笔有效 + 2 笔跳过
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


@pytest.fixture()
def finalize_delay(monkeypatch):
    mock = MagicMock(name="finalize_review.delay")
    monkeypatch.setattr(finalize_review, "delay", mock)
    return mock


@pytest.fixture()
def ingest_delay(monkeypatch):
    mock = MagicMock(name="ingest_transaction.delay")
    monkeypatch.setattr(ingest_transaction, "delay", mock)
    return mock


@pytest.fixture()
def reclassify_delay(monkeypatch):
    mock = MagicMock(name="reclassify_pending.delay")
    monkeypatch.setattr(reclassify_pending, "delay", mock)
    return mock


def make_item(session, counterparty: str = "星巴克") -> ReviewItem:
    txn = CanonicalTransaction(
        source=TxnSource.ALIPAY,
        direction=TxnDirection.EXPENSE,
        occurred_at=datetime(2026, 7, 25, 12, 30),
        amount=Decimal("35.00"),
        counterparty=counterparty,
        description="咖啡",
    )
    return review.create_review_item(
        session,
        txn,
        fingerprint=f"fp-{counterparty}",
        suggestion=ClassificationResult(category="餐饮", confidence=0.6, source="llm"),
        trace_id="trace-web",
    )


# ---------- 页面渲染 ----------


def test_console_page_empty_state(client):
    resp = client.get("/review")
    assert resp.status_code == 200
    assert "当前没有待复核项" in resp.text
    # 快捷记账与上传表单始终存在
    assert 'action="/review/quick"' in resp.text
    assert 'action="/review/upload"' in resp.text
    assert 'action="/review/query"' in resp.text
    assert 'name="viewport"' in resp.text


def test_console_page_lists_pending(client, db_session):
    item = make_item(db_session, counterparty="肯德基")
    resp = client.get("/review")
    assert resp.status_code == 200
    assert "肯德基" in resp.text
    assert "35.00" in resp.text
    assert "餐饮" in resp.text
    assert "0.60" in resp.text
    # 三个操作按钮齐全
    assert "批准" in resp.text
    assert "改分类" in resp.text
    assert "驳回" in resp.text
    assert f'action="/review/{item.id}/approve"' in resp.text
    assert f'action="/review/{item.id}/correct"' in resp.text
    assert f'action="/review/{item.id}/reject"' in resp.text


def test_console_page_escapes_merchant(client, db_session):
    make_item(db_session, counterparty="<script>alert(1)</script>")
    resp = client.get("/review")
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_console_card_shows_transaction_time(client, db_session):
    """卡片展示交易发生时间(occurred_at),而非上传入库时间。"""
    item = make_item(db_session)  # occurred_at=2026-07-25 12:30
    resp = client.get("/review")
    assert "2026-07-25 12:30" in resp.text
    # 上传时刻(created_at)不再展示
    db_session.refresh(item)
    assert item.created_at.strftime("%Y-%m-%d %H:%M") not in resp.text


def test_console_card_time_falls_back_to_created_at(client, db_session):
    """occurred_at 解析失败时回退 created_at。"""
    item = make_item(db_session)
    item.txn_payload = {**item.txn_payload, "occurred_at": "not-a-date"}
    db_session.flush()
    db_session.refresh(item)
    resp = client.get("/review")
    assert item.created_at.strftime("%Y-%m-%d %H:%M") in resp.text


def test_console_page_has_batch_buttons(client):
    resp = client.get("/review")
    assert 'action="/review/reclassify"' in resp.text
    assert 'action="/review/approve-all"' in resp.text
    assert "重新分类" in resp.text
    assert "全部批准" in resp.text
    # 全部批准需二次确认
    assert "confirm(" in resp.text


# ---------- 复核操作 ----------


def test_approve_flow(client, db_session, finalize_delay):
    item = make_item(db_session)
    resp = client.post(f"/review/{item.id}/approve", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/review?msg=")
    assert "已批准" in unquote_plus(resp.headers["location"])

    assert db_session.get(ReviewItem, item.id).status == ReviewStatus.APPROVED
    finalize_delay.assert_called_once_with(item.id)


def test_correct_flow(client, db_session, finalize_delay, reclassify_delay):
    item = make_item(db_session, counterparty="StarBucks")
    resp = client.post(
        f"/review/{item.id}/correct", data={"category": "购物"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "已改分类" in unquote_plus(resp.headers["location"])

    refreshed = db_session.get(ReviewItem, item.id)
    assert refreshed.status == ReviewStatus.CORRECTED
    assert refreshed.corrected_category == "购物"
    # 规则库回流一条 exact 规则
    rules = db_session.scalars(select(Rule)).all()
    assert len(rules) == 1
    assert rules[0].match_type == RuleMatchType.EXACT
    assert rules[0].merchant_pattern == "starbucks"
    assert rules[0].category == "购物"
    finalize_delay.assert_called_once_with(item.id)
    # 改正联动:触发规则模式重分类,清掉同商户其余 pending
    reclassify_delay.assert_called_once_with(rules_only=True)


def test_correct_failure_does_not_trigger_reclassify(
    client, db_session, finalize_delay, reclassify_delay
):
    resp = client.post("/review/9999/correct", data={"category": "购物"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "不存在" in unquote_plus(resp.headers["location"])
    finalize_delay.assert_not_called()
    reclassify_delay.assert_not_called()


def test_reject_flow(client, db_session, finalize_delay):
    item = make_item(db_session)
    resp = client.post(f"/review/{item.id}/reject", follow_redirects=False)
    assert resp.status_code == 303
    assert "已驳回" in unquote_plus(resp.headers["location"])

    assert db_session.get(ReviewItem, item.id).status == ReviewStatus.REJECTED
    # 驳回不写 Firefly
    finalize_delay.assert_not_called()


def test_duplicate_operation_redirects_with_error(client, db_session, finalize_delay):
    item = make_item(db_session)
    client.post(f"/review/{item.id}/approve", follow_redirects=False)
    # 重复批准:不 500,303 回跳并带错误提示
    resp = client.post(f"/review/{item.id}/approve", follow_redirects=False)
    assert resp.status_code == 303
    assert "仅 pending 可流转" in unquote_plus(resp.headers["location"])
    # 跟随重定向后页面展示该提示
    page = client.get(resp.headers["location"])
    assert page.status_code == 200
    assert "仅 pending 可流转" in page.text
    # delay 只在首次成功时触发
    finalize_delay.assert_called_once_with(item.id)


def test_operation_on_missing_item_redirects(client, finalize_delay):
    resp = client.post("/review/9999/reject", follow_redirects=False)
    assert resp.status_code == 303
    assert "不存在" in unquote_plus(resp.headers["location"])
    finalize_delay.assert_not_called()


# ---------- 批量操作 ----------


def test_approve_all_flow(client, db_session, finalize_delay):
    a = make_item(db_session, counterparty="A店")
    b = make_item(db_session, counterparty="B店")
    resp = client.post("/review/approve-all", follow_redirects=False)
    assert resp.status_code == 303
    assert "已批准 2 笔" in unquote_plus(resp.headers["location"])

    assert db_session.get(ReviewItem, a.id).status == ReviewStatus.APPROVED
    assert db_session.get(ReviewItem, b.id).status == ReviewStatus.APPROVED
    # 每条各触发一次 finalize
    assert finalize_delay.call_count == 2
    assert {call.args[0] for call in finalize_delay.call_args_list} == {a.id, b.id}


def test_approve_all_empty(client, finalize_delay):
    resp = client.post("/review/approve-all", follow_redirects=False)
    assert resp.status_code == 303
    assert "已批准 0 笔" in unquote_plus(resp.headers["location"])
    finalize_delay.assert_not_called()


def test_reclassify_endpoint_triggers_task(client, reclassify_delay):
    resp = client.post("/review/reclassify", follow_redirects=False)
    assert resp.status_code == 303
    assert "已触发" in unquote_plus(resp.headers["location"])
    reclassify_delay.assert_called_once_with(rules_only=False)


# ---------- 快捷记账 ----------


def test_quick_expense_enqueued(client, ingest_delay):
    resp = client.post("/review/quick", data={"text": "早餐 15"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "已收到" in unquote_plus(resp.headers["location"])

    ingest_delay.assert_called_once()
    payload, trace_id = ingest_delay.call_args.args
    assert payload["counterparty"] == "早餐"
    assert payload["amount"] == "15"
    assert payload["source"] == "manual"
    assert trace_id


def test_quick_expense_unparseable_redirects(client, ingest_delay):
    resp = client.post("/review/quick", data={"text": "只有描述没有金额"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "没看懂" in unquote_plus(resp.headers["location"])
    ingest_delay.assert_not_called()


# ---------- 自然语言查账 ----------


def test_finance_query_returns_deterministic_sum(client, monkeypatch):
    from app.api import routes_review

    class FakeLLM:
        def parse_finance_query(self, question, today):
            assert question == "上月餐饮花了多少"
            assert today == date.today()
            return FinanceQuery(
                start=date(2026, 6, 1),
                end=date(2026, 6, 30),
                category="餐饮",
            )

    class FakeFirefly:
        def list_transactions(self, start, end, txn_type):
            assert (start, end, txn_type) == (
                date(2026, 6, 1),
                date(2026, 6, 30),
                "withdrawal",
            )
            return [
                {
                    "date": "2026-06-02",
                    "amount": "12.10",
                    "destination_name": "餐厅",
                    "category_name": "餐饮",
                },
                {
                    "date": "2026-06-03",
                    "amount": "7.90",
                    "destination_name": "咖啡店",
                    "category_name": "餐饮",
                },
            ]

    monkeypatch.setattr(routes_review, "get_llm_client", lambda: FakeLLM())
    monkeypatch.setattr(routes_review, "get_firefly_client", lambda: FakeFirefly())

    resp = client.post(
        "/review/query",
        data={"question": "上月餐饮花了多少"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    message = unquote_plus(resp.headers["location"])
    assert "已理解:" in message
    assert "2026-06-01 至 2026-06-30" in message
    assert "支出" in message
    assert "分类「餐饮」" in message
    assert "金额合计" in message
    assert "查询结果:合计 20.00 CNY" in message


def test_finance_query_llm_error_is_shown(client, monkeypatch):
    from app.api import routes_review
    from app.llm.client import LLMError

    class FakeLLM:
        def parse_finance_query(self, question, today):
            raise LLMError("不支持的问题")

    monkeypatch.setattr(routes_review, "get_llm_client", lambda: FakeLLM())

    resp = client.post(
        "/review/query",
        data={"question": "帮我推荐股票"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert "没能识别查询条件" in unquote_plus(resp.headers["location"])


def test_finance_query_network_error_is_shown(client, monkeypatch):
    from app.api import routes_review

    class FakeLLM:
        def parse_finance_query(self, question, today):
            return FinanceQuery(start=date(2026, 6, 1), end=date(2026, 6, 30))

    class OfflineFirefly:
        def list_transactions(self, start, end, txn_type):
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(routes_review, "get_llm_client", lambda: FakeLLM())
    monkeypatch.setattr(routes_review, "get_firefly_client", lambda: OfflineFirefly())

    resp = client.post(
        "/review/query",
        data={"question": "上月花了多少"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert "账目服务暂时不可用" in unquote_plus(resp.headers["location"])


# ---------- CSV 上传表单 ----------


def test_upload_form_enqueues(client, ingest_delay):
    resp = client.post(
        "/review/upload",
        data={"source": "alipay"},
        files={"file": ("bill.csv", ALIPAY_CSV, "text/csv")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    msg = unquote_plus(resp.headers["location"])
    assert "已入队 2 笔" in msg
    assert "跳过 2 笔" in msg
    assert ingest_delay.call_count == 2
    first = ingest_delay.call_args_list[0].args[0]
    assert first["source"] == "alipay"
    assert first["counterparty"] == "某某餐厅"


def test_upload_form_auto_option_default(client):
    """渠道下拉第一项为「自动识别」且默认选中。"""
    resp = client.get("/review")
    assert '<option value="auto" selected>自动识别</option>' in resp.text
    assert resp.text.index('value="auto"') < resp.text.index('value="alipay"')


def test_upload_form_auto_detects_and_reports_source(client, ingest_delay):
    """source=auto:自动识别渠道,成功提示带中文渠道名。"""
    resp = client.post(
        "/review/upload",
        data={"source": "auto"},
        files={"file": ("bill.csv", ALIPAY_CSV, "text/csv")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    msg = unquote_plus(resp.headers["location"])
    assert "已入队 2 笔" in msg
    assert "渠道:支付宝" in msg
    assert ingest_delay.call_count == 2
    assert ingest_delay.call_args_list[0].args[0]["source"] == "alipay"


def test_upload_form_auto_unrecognized_redirects(client, ingest_delay):
    resp = client.post(
        "/review/upload",
        data={"source": "auto"},
        files={"file": ("bill.csv", b"hello,world\n1,2\n", "text/csv")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    msg = unquote_plus(resp.headers["location"])
    assert "账单解析失败" in msg
    assert "无法自动识别账单渠道" in msg
    ingest_delay.assert_not_called()


def test_upload_form_invalid_source_redirects(client, ingest_delay):
    resp = client.post(
        "/review/upload",
        data={"source": "bank"},
        files={"file": ("bill.csv", ALIPAY_CSV, "text/csv")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "不支持的账单来源" in unquote_plus(resp.headers["location"])
    ingest_delay.assert_not_called()


def test_upload_form_bad_file_redirects(client, ingest_delay):
    resp = client.post(
        "/review/upload",
        data={"source": "alipay"},
        files={"file": ("bill.csv", b"hello,world\n1,2\n", "text/csv")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "账单解析失败" in unquote_plus(resp.headers["location"])
    ingest_delay.assert_not_called()


# ---------- 原 JSON 上传端点行为不变(抽取共享函数后回归) ----------


def test_json_upload_endpoint_unchanged(client, ingest_delay):
    resp = client.post(
        "/api/upload/csv",
        params={"source": "alipay"},
        files={"file": ("bill.csv", ALIPAY_CSV, "text/csv")},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["enqueued"] == 2
    assert body["skipped"] == 2
    assert ingest_delay.call_count == 2

    assert (
        client.post(
            "/api/upload/csv",
            params={"source": "bank"},
            files={"file": ("bill.csv", ALIPAY_CSV, "text/csv")},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/upload/csv",
            params={"source": "alipay"},
            files={"file": ("bill.csv", b"\x80\x80\x80", "text/csv")},
        ).status_code
        == 400
    )


# ---------- token 鉴权 ----------


def test_console_token_auth(client, monkeypatch):
    # conftest 未设 CONSOLE_TOKEN;直接改缓存单例属性,monkeypatch 结束后自动恢复为 ""
    monkeypatch.setattr(get_settings(), "console_token", "sekret")

    # 裸访问 / 错误 token / 无 cookie 的 POST:一律 401 提示页
    assert client.get("/review", follow_redirects=False).status_code == 401
    assert (
        client.get("/review", params={"token": "wrong"}, follow_redirects=False).status_code == 401
    )
    assert client.post("/review/1/approve", follow_redirects=False).status_code == 401

    # 带正确 ?token=:种 httponly cookie 并 303 回 /review
    resp = client.get("/review", params={"token": "sekret"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/review"
    set_cookie = resp.headers.get("set-cookie", "")
    assert "console_token=sekret" in set_cookie
    assert "HttpOnly" in set_cookie

    # 此后凭 cookie 通过(TestClient 自动带上 cookie)
    assert client.get("/review").status_code == 200


def test_console_open_when_token_empty(client):
    # 默认 console_token 为空:本机模式不设防
    assert get_settings().console_token == ""
    assert client.get("/review").status_code == 200
