import asyncio
import json

import httpx
import pytest
from test_api import MODEL, FakeUpstream

from m365_agent_gateway.api import create_app
from m365_agent_gateway.config import Settings
from m365_agent_gateway.errors import GatewayError
from m365_agent_gateway.normalize import normalize
from m365_agent_gateway.tools import Tool, decode_reply


async def test_deadline_survives_multiple_stream_tasks():
    class StalledUpstream:
        async def stream(self, prompt, tone, images=()):
            yield "first"
            await asyncio.sleep(0.1)
            yield "too late"

    app = create_app(Settings(timeout=0.02), StalledUpstream())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        result = await client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert "upstream_timeout" in result.text
        assert "too late" not in result.text


async def test_anthropic_context_limit_uses_compactable_error():
    app = create_app(Settings(max_prompt=50), FakeUpstream())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        result = await client.post(
            "/v1/messages",
            json={
                "model": MODEL,
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "x" * 500}],
            },
        )
    assert result.status_code == 400
    error = result.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["message"].startswith("prompt is too long")


async def test_chat_stream_error_has_choices():
    class FailingUpstream:
        async def stream(self, prompt, tone, images=()):
            raise GatewayError("Copilot rejected request.", "upstream_rejected", 502)
            yield ""

    app = create_app(Settings(), FailingUpstream())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        result = await client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
    events = [
        json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: {")
    ]
    assert all(event.get("choices") for event in events)
    assert "upstream_rejected" in events[-1]["choices"][0]["delta"]["content"]
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.parametrize(
    "patch",
    [
        {"tools": None},
        {"thinking": []},
        {"response_format": []},
        {"tools": [{"type": "function", "function": []}]},
        {"messages": [{"role": "assistant", "tool_calls": [42]}]},
    ],
)
def test_type_confusion_becomes_public_error(patch):
    with pytest.raises(GatewayError):
        normalize({"messages": [{"role": "user", "content": "hi"}], **patch}, "chat", Settings())


def test_unresolvable_schema_fails_closed():
    tool = Tool("read", "", {"type": "object", "$ref": "#/$defs/missing"})
    with pytest.raises(GatewayError):
        decode_reply('{"text":"","calls":[{"name":"read","arguments":{}}]}', [tool])


def test_schema_identifier_cannot_enable_remote_refs():
    with pytest.raises(GatewayError):
        Tool("read", "", {"$id": "https://evil.test/schema", "$ref": "https://evil.test/thing"})


async def test_count_tokens_type_confusion_is_400():
    app = create_app(Settings(), FakeUpstream())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        result = await client.post(
            "/v1/messages/count_tokens",
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": None,
            },
        )
        assert result.status_code == 400


async def test_nonfinite_json_rejected():
    app = create_app(Settings(), FakeUpstream())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        result = await client.post(
            "/v1/chat/completions",
            content=('{"messages":[{"role":"user","content":"hi"}],"temperature":NaN}'),
        )
        assert result.status_code == 400


def test_timeout_env_is_bounded(monkeypatch):
    for value in ["nan", "inf", "-1", "invalid"]:
        monkeypatch.setenv("M365_TIMEOUT", value)
        with pytest.raises(GatewayError):
            Settings.from_env()
