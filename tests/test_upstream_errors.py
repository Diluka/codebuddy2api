"""Replay observed upstream rejections through the managed API boundary."""
import asyncio
import json
import time

import httpx
import pytest

from admin.server import create_app
from core import converter
from core.credentials import CredentialManager


@pytest.fixture
def rejection_api(tmp_path, monkeypatch):
    real_client = httpx.AsyncClient
    previous = CredentialManager.current()
    previous_config = dict(converter.CONFIG)
    monkeypatch.setattr(converter, "_check_auth", converter._check_auth)
    auth = tmp_path / "auth"
    auth.mkdir()
    (auth / "fixture.info").write_text(json.dumps({
        "account": {"uid": "fixture-user"},
        "auth": {"accessToken": "fixture-only", "refreshToken": "fixture-only",
                 "expiresAt": (time.time() + 3600) * 1000},
    }))
    app = create_app(tmp_path / "management", auth, "test-api-key", "test-admin-key-long-enough")
    calls = []

    def connect(status, envelope):
        def upstream(request):
            calls.append(request)
            return httpx.Response(status, json=envelope)

        monkeypatch.setattr(converter.httpx, "AsyncClient", lambda **kw: real_client(
            transport=httpx.MockTransport(upstream), **kw))
        return real_client(transport=httpx.ASGITransport(app=app), base_url="https://test",
                           headers={"Authorization": "Bearer test-api-key"})

    try:
        yield connect, app, calls
    finally:
        CredentialManager.set_current(previous)
        converter.CONFIG.clear()
        converter.CONFIG.update(previous_config)


def rejection(code):
    if code == 11140:
        return {
            "code": 11140,
            "msg": "request illegal",
            "requestId": "1912792c-d753-4d26-accd-b37a7c9ff4c1",
            "retry_policy": {"retry": False},
            "displayMsg": {"en": "Your account has been restricted due to unusual activity", "zh": "账号存在异常行为，已被限制使用"},
            "displayTips": {
                "en": "We detected unusual activity on this account. Submit feedback to appeal this restriction",
                "zh": "系统检测到该账号存在异常使用行为，如需申诉请提交反馈",
            },
        }
    return {
        "code": code,
        "msg": "Illegal API invocation from an unapproved channel",
        "requestId": "4d72f21e-6c26-497e-aa86-1199f1c964a7",
        "retry_policy": {"retry": False},
        "displayMsg": {"en": "Request blocked. Please send it again", "zh": "请求被拦截，请重新发送"},
        "displayTips": {
            "en": "This call failed channel verification, possibly because it was not sent from an official client. Please resend from the official client.",
            "zh": "这次调用没通过渠道校验，可能不是官方客户端发出的。请用官方版本重新发送。",
            "zh-hant": "本次呼叫未通過通路驗證，可能不是官方用戶端發出。請使用官方版本重新傳送。",
        },
    }


@pytest.mark.parametrize("status,code", [(400, 11128), (403, 11140)])
@pytest.mark.parametrize("path,stream", [
    ("/v1/chat/completions", True),
    ("/v1/responses", True),
    ("/v1/messages", True),
    ("/v1/chat/completions", False),
    ("/v1/responses", False),
    ("/v1/messages", False),
    ("/v1/messages/count_tokens", False),
])
def test_rejection_retains_business_error_without_replay(rejection_api, status, code, path, stream):
    connect, app, calls = rejection_api
    envelope = rejection(code)

    async def run():
        async with connect(status, envelope) as client:
            body = {"model": "deepseek-v4.1-flash", "stream": stream}
            if path == "/v1/responses":
                body["input"] = "Reply OK."
            else:
                body["messages"] = [{"role": "user", "content": "Reply OK."}]
            response = await client.post(path, json=body)
        if stream:
            assert response.status_code == 200  # SSE headers precede the upstream rejection.
            events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
            assert len(events) == 1
            event = events[0]
            if path == "/v1/responses":
                assert event["type"] == "response.failed"
                error = event["response"]["error"]
            else:
                error = event["error"]
        else:
            assert response.status_code == status
            detail = response.json()["detail"]
            error = detail.get("error", detail)
        assert int(error["code"]) == code
        assert error["requestId"] == envelope["requestId"]
        assert error["retry_policy"] == {"retry": False}
        assert error["displayTips"] == envelope["displayTips"]
        assert len(calls) == 1
        if stream or path in ("/v1/messages", "/v1/messages/count_tokens"):
            assert error["http_status"] == status
            assert error["message"] == envelope["msg"]
        if status == 403:
            aid = next(iter(app.state.store.data["accounts"]))
            assert app.state.store.data["account_status"][aid]["cooldown_until"] > time.time()
        if path != "/v1/messages/count_tokens":
            metrics = app.state.metrics.snapshot()
            assert metrics["succeeded"] == 0
            assert metrics["failed"] == 1

    asyncio.run(run())
