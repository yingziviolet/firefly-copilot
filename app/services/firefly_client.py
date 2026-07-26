"""Firefly III REST API 客户端(httpx 同步,Bearer PAT)。

关键点:
- store_transaction 必须带 external_id=指纹,Firefly 侧二次防重
- 所有请求带 Accept: application/json;错误抛 FireflyError
"""

from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx

from app.config import get_settings
from app.logger import get_logger
from app.schemas.transaction import CanonicalTransaction, TxnDirection

logger = get_logger(__name__)

# 错误信息里响应体的最大截断长度
_BODY_SUMMARY_LEN = 200


class FireflyError(RuntimeError):
    pass


class FireflyClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.firefly_base_url).rstrip("/")
        self.token = token or settings.firefly_pat
        self._client = client or httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """统一请求入口:拼 URL、带认证头;非 2xx 抛 FireflyError(含状态码与响应体摘要)。"""
        url = f"{self.base_url}{path}"
        resp = self._client.request(method, url, headers=self._headers(), **kwargs)
        if not resp.is_success:
            summary = resp.text[:_BODY_SUMMARY_LEN]
            raise FireflyError(
                f"Firefly API {method} {path} failed: HTTP {resp.status_code}, body={summary!r}"
            )
        return resp

    def _iter_pages(
        self, path: str, params: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        """按页迭代 GET 结果:优先看 meta.pagination.total_pages,退化用 links.next。"""
        page = 1
        while True:
            merged = dict(params or {})
            merged["page"] = page
            payload = self._request("GET", path, params=merged).json()
            yield payload
            pagination = (payload.get("meta") or {}).get("pagination") or {}
            total_pages = pagination.get("total_pages")
            current = pagination.get("current_page", page)
            if total_pages is not None:
                if current >= total_pages:
                    return
            elif not (payload.get("links") or {}).get("next"):
                return
            page = current + 1

    def ping(self) -> bool:
        """GET /api/v1/about,200 即通。"""
        # 健康探测语义:任何失败(非 2xx / 网络异常)都返回 False,不抛错
        try:
            resp = self._client.get(f"{self.base_url}/api/v1/about", headers=self._headers())
        except httpx.HTTPError as exc:
            logger.warning("firefly_ping_failed", error=str(exc))
            return False
        return resp.is_success

    def list_categories(self) -> list[str]:
        """GET /api/v1/categories(翻页取全)-> 分类名列表。"""
        names: list[str] = []
        for payload in self._iter_pages("/api/v1/categories"):
            for item in payload.get("data", []):
                name = (item.get("attributes") or {}).get("name")
                if name:
                    names.append(name)
        return names

    def store_transaction(
        self,
        txn: CanonicalTransaction,
        category: str | None,
        external_id: str,
        asset_account: str | None = None,
    ) -> str:
        """POST /api/v1/transactions,返回 Firefly 交易 id(字符串)。

        withdrawal: source=资产账户, destination=counterparty(expense account 自动建)
        deposit:    source=counterparty(revenue), destination=资产账户
        error_if_duplicate_hash 设 False(我们自己去重),external_id 必填。
        """
        settings = get_settings()
        asset = asset_account or settings.default_asset_account
        split: dict[str, Any] = {
            "type": txn.direction.value,
            "date": txn.occurred_at.isoformat(),
            "amount": str(txn.amount),
            # Firefly 要求 description 非空,空描述回退到交易对方
            "description": txn.description or txn.counterparty,
            "external_id": external_id,
            "currency_code": txn.currency,
        }
        if category:
            split["category_name"] = category
        if txn.direction is TxnDirection.INCOME:
            split["source_name"] = txn.counterparty
            split["destination_name"] = asset
        else:
            split["source_name"] = asset
            split["destination_name"] = txn.counterparty
        body = {
            "error_if_duplicate_hash": False,
            "apply_rules": True,
            "transactions": [split],
        }
        payload = self._request("POST", "/api/v1/transactions", json=body).json()
        txn_id = (payload.get("data") or {}).get("id")
        if txn_id is None:
            raise FireflyError(
                f"Firefly API POST /api/v1/transactions: missing data.id, "
                f"body={str(payload)[:_BODY_SUMMARY_LEN]!r}"
            )
        logger.info("firefly_txn_stored", firefly_id=str(txn_id), external_id=external_id)
        return str(txn_id)

    def list_transactions(
        self, start: date, end: date, txn_type: str = "withdrawal"
    ) -> list[dict[str, Any]]:
        """GET /api/v1/transactions?start=&end=&type=(翻页取全)-> 扁平化交易 split 列表。

        每个元素至少含:description, amount(str), date, destination_name, transaction_journal_id。
        """
        params = {"start": start.isoformat(), "end": end.isoformat(), "type": txn_type}
        splits: list[dict[str, Any]] = []
        for payload in self._iter_pages("/api/v1/transactions", params):
            for item in payload.get("data", []):
                # 每条 transaction 的 attributes.transactions 是 split 数组,逐个扁平化
                splits.extend((item.get("attributes") or {}).get("transactions", []))
        return splits


def get_firefly_client() -> FireflyClient:
    return FireflyClient()
