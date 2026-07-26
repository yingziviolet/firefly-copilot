"""FireflyClient 单测:全部走 respx mock,不触真实网络。"""

import json
from datetime import date, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from app.schemas.transaction import CanonicalTransaction, TxnDirection, TxnSource
from app.services.firefly_client import FireflyClient, FireflyError

BASE = "http://firefly.test"


def make_client() -> FireflyClient:
    # 显式传入 httpx.Client,respx.mock 会全局拦截其传输层
    return FireflyClient(client=httpx.Client())


def make_txn(direction: TxnDirection = TxnDirection.EXPENSE, **kw) -> CanonicalTransaction:
    data: dict = {
        "source": TxnSource.ALIPAY,
        "direction": direction,
        "occurred_at": datetime(2026, 7, 25, 12, 30),
        "amount": Decimal("23.50"),
        "currency": "CNY",
        "counterparty": "肯德基",
        "description": "午餐",
    }
    data.update(kw)
    return CanonicalTransaction(**data)


@respx.mock
def test_ping_ok_and_headers():
    route = respx.get(f"{BASE}/api/v1/about").mock(
        return_value=httpx.Response(200, json={"data": {"version": "6.1"}})
    )
    assert make_client().ping() is True
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer test-pat"
    assert req.headers["Accept"] == "application/json"


@respx.mock
def test_ping_non_2xx_returns_false():
    respx.get(f"{BASE}/api/v1/about").mock(return_value=httpx.Response(500, text="oops"))
    assert make_client().ping() is False


@respx.mock
def test_ping_network_error_returns_false():
    respx.get(f"{BASE}/api/v1/about").mock(side_effect=httpx.ConnectError("boom"))
    assert make_client().ping() is False


@respx.mock
def test_store_transaction_withdrawal_body():
    route = respx.post(f"{BASE}/api/v1/transactions").mock(
        return_value=httpx.Response(200, json={"data": {"id": "42"}})
    )
    fid = make_client().store_transaction(make_txn(), category="餐饮", external_id="fp-001")
    assert fid == "42"
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer test-pat"
    assert req.headers["Content-Type"] == "application/json"
    body = json.loads(req.content)
    assert body["error_if_duplicate_hash"] is False
    assert body["apply_rules"] is True
    assert len(body["transactions"]) == 1
    split = body["transactions"][0]
    assert split["type"] == "withdrawal"
    assert split["external_id"] == "fp-001"
    assert split["amount"] == "23.50"
    assert split["currency_code"] == "CNY"
    assert split["category_name"] == "餐饮"
    assert split["date"].startswith("2026-07-25T12:30")
    # withdrawal:资产账户 -> 商户;资产账户缺省取 settings.default_asset_account
    assert split["source_name"] == "现金钱包"
    assert split["destination_name"] == "肯德基"


@respx.mock
def test_store_transaction_deposit_direction_and_no_category():
    route = respx.post(f"{BASE}/api/v1/transactions").mock(
        return_value=httpx.Response(200, json={"data": {"id": "7"}})
    )
    txn = make_txn(direction=TxnDirection.INCOME, counterparty="某公司")
    fid = make_client().store_transaction(
        txn, category=None, external_id="fp-002", asset_account="工资卡"
    )
    assert fid == "7"
    split = json.loads(route.calls.last.request.content)["transactions"][0]
    assert split["type"] == "deposit"
    # deposit:商户(revenue)-> 资产账户
    assert split["source_name"] == "某公司"
    assert split["destination_name"] == "工资卡"
    assert "category_name" not in split


@respx.mock
def test_store_transaction_non_2xx_raises_firefly_error():
    respx.post(f"{BASE}/api/v1/transactions").mock(
        return_value=httpx.Response(422, json={"message": "Invalid data"})
    )
    with pytest.raises(FireflyError) as ei:
        make_client().store_transaction(make_txn(), category=None, external_id="fp-003")
    msg = str(ei.value)
    assert "422" in msg
    assert "Invalid data" in msg


@respx.mock
def test_list_categories_pagination_meta():
    def responder(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        names = {1: ["餐饮", "交通"], 2: ["娱乐"]}[page]
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": str(i), "attributes": {"name": n}} for i, n in enumerate(names)
                ],
                "meta": {"pagination": {"total_pages": 2, "current_page": page}},
            },
        )

    route = respx.get(f"{BASE}/api/v1/categories").mock(side_effect=responder)
    assert make_client().list_categories() == ["餐饮", "交通", "娱乐"]
    assert route.call_count == 2


@respx.mock
def test_list_categories_non_2xx_raises():
    respx.get(f"{BASE}/api/v1/categories").mock(
        return_value=httpx.Response(401, text="Unauthenticated")
    )
    with pytest.raises(FireflyError) as ei:
        make_client().list_categories()
    assert "401" in str(ei.value)


def _split(journal_id: str, desc: str, amount: str) -> dict:
    return {
        "description": desc,
        "amount": amount,
        "date": "2026-07-02T00:00:00+08:00",
        "destination_name": "商户",
        "transaction_journal_id": journal_id,
    }


@respx.mock
def test_list_transactions_flatten_and_links_next_pagination():
    def responder(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert params["start"] == "2026-07-01"
        assert params["end"] == "2026-07-25"
        assert params["type"] == "withdrawal"
        page = int(params.get("page", "1"))
        if page == 1:
            # 一条交易含两个 split + 无 meta,仅 links.next 提示还有下一页
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "attributes": {
                                "transactions": [
                                    _split("101", "a", "1.00"),
                                    _split("102", "b", "2.00"),
                                ]
                            }
                        }
                    ],
                    "links": {"next": f"{BASE}/api/v1/transactions?page=2"},
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [{"attributes": {"transactions": [_split("103", "c", "3.00")]}}],
                "links": {},
            },
        )

    route = respx.get(f"{BASE}/api/v1/transactions").mock(side_effect=responder)
    rows = make_client().list_transactions(date(2026, 7, 1), date(2026, 7, 25))
    assert route.call_count == 2
    assert [r["transaction_journal_id"] for r in rows] == ["101", "102", "103"]
    assert rows[0]["description"] == "a"
    assert rows[0]["amount"] == "1.00"
    assert rows[0]["destination_name"] == "商户"
