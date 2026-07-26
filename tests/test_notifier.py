"""notifier 单测:respx 拦截 Telegram sendMessage,不触真实网络。"""

import json

import httpx
import respx

from app.config import get_settings
from app.services.notifier import send_telegram_message

# conftest 中的假 token
SEND_URL = "https://api.telegram.org/bot123456:test-token/sendMessage"


@respx.mock
def test_send_success_uses_default_chat_id():
    route = respx.post(SEND_URL).mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )
    assert send_telegram_message("hello") is True
    assert route.called
    payload = json.loads(route.calls.last.request.read())
    # 缺省 chat_id 取 settings.telegram_alert_chat_id
    assert payload == {"chat_id": "10001", "text": "hello"}


@respx.mock
def test_send_success_with_explicit_chat_id_and_parse_mode():
    route = respx.post(SEND_URL).mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    assert send_telegram_message("<b>hi</b>", chat_id="20002", parse_mode="HTML") is True
    payload = json.loads(route.calls.last.request.read())
    assert payload["chat_id"] == "20002"
    assert payload["parse_mode"] == "HTML"


@respx.mock
def test_telegram_4xx_returns_false():
    respx.post(SEND_URL).mock(
        return_value=httpx.Response(
            400, json={"ok": False, "description": "Bad Request: chat not found"}
        )
    )
    assert send_telegram_message("hello") is False


@respx.mock
def test_network_error_returns_false():
    respx.post(SEND_URL).mock(side_effect=httpx.ConnectError("boom"))
    assert send_telegram_message("hello") is False


@respx.mock
def test_missing_token_returns_false_without_request(monkeypatch):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    monkeypatch.setattr(get_settings(), "telegram_bot_token", "")
    assert send_telegram_message("hello") is False
    assert not route.called


@respx.mock
def test_missing_chat_id_returns_false_without_request(monkeypatch):
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    monkeypatch.setattr(get_settings(), "telegram_alert_chat_id", "")
    assert send_telegram_message("hello") is False
    assert not route.called
