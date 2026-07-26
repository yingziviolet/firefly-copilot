"""notifier 单测:respx 拦截企微 HTTP,不触真实网络。"""

import json

import httpx
import pytest
import respx

from app.config import get_settings
from app.services.notifier import notify, send_wecom_message

WECOM_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key"


@pytest.fixture()
def wecom_configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "wecom_webhook_url", WECOM_URL)


@respx.mock
def test_wecom_success(wecom_configured):
    route = respx.post(WECOM_URL).mock(
        return_value=httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})
    )
    assert send_wecom_message("hello") is True
    assert route.called
    payload = json.loads(route.calls.last.request.read())
    assert payload == {"msgtype": "text", "text": {"content": "hello"}}


@respx.mock
def test_wecom_errcode_nonzero_returns_false(wecom_configured):
    respx.post(WECOM_URL).mock(
        return_value=httpx.Response(200, json={"errcode": 93000, "errmsg": "invalid key"})
    )
    assert send_wecom_message("hello") is False


@respx.mock
def test_wecom_http_500_returns_false(wecom_configured):
    respx.post(WECOM_URL).mock(return_value=httpx.Response(500, text="boom"))
    assert send_wecom_message("hello") is False


@respx.mock
def test_wecom_non_json_body_returns_false(wecom_configured):
    respx.post(WECOM_URL).mock(return_value=httpx.Response(200, text="not-json"))
    assert send_wecom_message("hello") is False


@respx.mock
def test_wecom_network_error_returns_false(wecom_configured):
    respx.post(WECOM_URL).mock(side_effect=httpx.ConnectError("boom"))
    assert send_wecom_message("hello") is False


def test_wecom_not_configured_returns_false():
    # conftest 未设置 WECOM_WEBHOOK_URL
    assert send_wecom_message("hello") is False


@respx.mock
def test_notify_delegates_to_wecom(wecom_configured):
    respx.post(WECOM_URL).mock(return_value=httpx.Response(200, json={"errcode": 0}))
    assert notify("alert") is True


def test_notify_no_channel_returns_false():
    assert notify("alert") is False
