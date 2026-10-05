import asyncio
import json
import time
from importlib.metadata import version

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
    assert events[-1] == {
        "error": {
            "message": "Copilot rejected request.",
            "type": "upstream_rejected",
            "code": "upstream_rejected",
        }
    }
    assert "[DONE]" not in result.text


async def test_chat_stream_midstream_failure_is_error_event():
    class Exploding:
        async def stream(self, prompt, tone, images=()):
            yield "partial"
            raise RuntimeError("boom")

    app = create_app(Settings(), Exploding())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        result = await client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
    last = json.loads(result.text.strip().splitlines()[-1][6:])
    assert last["error"]["code"] == "gateway_error"
    assert "boom" not in result.text and "[DONE]" not in result.text


async def test_responses_stream_failure_emits_response_failed():
    class Failing:
        async def stream(self, prompt, tone, images=()):
            yield "partial"
            raise GatewayError("Copilot rejected request.", "upstream_rejected", 502)

    app = create_app(Settings(), Failing())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        result = await client.post(
            "/v1/responses", json={"model": MODEL, "stream": True, "input": "hi"}
        )
    events = [
        json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: {")
    ]
    error, failed = events[-2:]
    assert error["type"] == "error" and error["code"] == "upstream_rejected"
    assert error["param"] is None and failed["type"] == "response.failed"
    assert failed["response"]["status"] == "failed"
    assert failed["response"]["error"] == {
        "code": "upstream_rejected",
        "message": "Copilot rejected request.",
    }
    sequence = [event["sequence_number"] for event in events]
    assert sequence == list(range(len(events)))


@pytest.mark.parametrize(
    "patch",
    [
        {"tools": 5},
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
                "tools": 5,
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


def request(**patch):
    return {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], **patch}


def test_explicit_nulls_use_defaults():
    nulls = dict.fromkeys(
        [
            "model",
            "n",
            "thinking",
            "response_format",
            "tools",
            "tool_choice",
            "stream",
            "max_tokens",
            "max_completion_tokens",
            "max_output_tokens",
            "system",
            "instructions",
            "parallel_tool_calls",
        ]
    )
    turn = normalize(request(**nulls), "chat", Settings())
    assert turn.parallel and not turn.stream and not turn.tools and turn.choice == "auto"


def test_output_budget_precedence():
    both = {"max_completion_tokens": 10, "max_tokens": 20, "max_output_tokens": 30}
    assert normalize(request(**both), "chat", Settings()).max_chars == 40
    del both["max_completion_tokens"]
    assert normalize(request(**both), "chat", Settings()).max_chars == 80
    assert normalize(request(max_output_tokens=30), "chat", Settings()).max_chars == 120


def test_message_cap_is_context_limit_and_empty_is_generic():
    with pytest.raises(GatewayError) as caught:
        normalize(request(messages=[{"role": "user", "content": "x"}] * 513), "chat", Settings())
    assert caught.value.code == "context_limit" and caught.value.status == 413
    with pytest.raises(GatewayError) as caught:
        normalize(request(messages=[]), "chat", Settings())
    assert caught.value.code == "invalid_request"


def test_anthropic_tool_result_accepts_images():
    block = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "aW1n"},
    }
    body = request(
        messages=[
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t", "content": [block]}],
            }
        ]
    )
    turn = normalize(body, "anthropic", Settings())
    assert len(turn.images) == 1 and "[Image attached]" in turn.messages[0]["content"]


def test_image_count_is_capped():
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1n"}}
    content = [image] * 21
    with pytest.raises(GatewayError, match="At most 20 images"):
        normalize(request(messages=[{"role": "user", "content": content}]), "chat", Settings())
    assert (
        len(
            normalize(
                request(messages=[{"role": "user", "content": [image] * 20}]), "chat", Settings()
            ).images
        )
        == 20
    )


async def test_historical_tool_input_may_contain_ref_key():
    app = create_app(Settings(), FakeUpstream())
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "x", "input": {"$ref": "https://a.b"}}
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        },
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        result = await client.post(
            "/v1/messages", json={"model": MODEL, "max_tokens": 10, "messages": messages}
        )
    assert result.status_code == 200


async def test_streaming_validation_errors_are_http_errors():
    app = create_app(Settings(), FakeUpstream())
    body = request(
        max_tokens=10,
        stream=True,
        tools=[{"name": "a", "input_schema": {"type": "object"}}],
        tool_choice={"type": "tool", "name": "missing"},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        result = await client.post("/v1/messages", json=body)
    assert result.status_code == 400 and result.json()["type"] == "error"
    assert not result.headers["content-type"].startswith("text/event-stream")


async def test_streaming_prompt_over_frame_limit_is_413(monkeypatch):
    monkeypatch.setattr("m365_agent_gateway.service.MAX_FRAME_BYTES", 100)
    app = create_app(Settings(), FakeUpstream())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        result = await client.post("/v1/chat/completions", json=request(stream=True))
    assert result.status_code == 413 and "2 MB" in result.json()["error"]["message"]


async def test_anthropic_error_types_follow_status():
    app = create_app(Settings(rate_per_minute=1, max_body=200), FakeUpstream())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        big = await client.post("/v1/messages", content="x" * 300)
        assert big.status_code == 413
        assert big.json()["error"]["type"] == "request_too_large"
        assert big.json()["type"] == "error"
        await client.post("/v1/messages", json={})
        limited = await client.post("/v1/messages", json={})
        assert limited.status_code == 429
        assert limited.json()["error"]["type"] == "rate_limit_error"
        chat = await client.post("/v1/chat/completions", json={})
        assert chat.json()["error"]["type"] == "rate_limit"


async def test_health_reports_installed_version():
    app = create_app(Settings(), FakeUpstream())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        assert (await client.get("/health")).json()["version"] == version("m365-agent-gateway")


async def responses_client(**settings):
    app = create_app(Settings(**settings), FakeUpstream())
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")
    return client


async def test_responses_store_defaults_to_true_and_false_opts_out():
    async with await responses_client() as client:
        stored = (await client.post("/v1/responses", json={"model": MODEL, "input": "a"})).json()
        again = await client.post(
            "/v1/responses",
            json={"model": MODEL, "input": "b", "previous_response_id": stored["id"]},
        )
        assert again.status_code == 200
        hidden = (
            await client.post("/v1/responses", json={"model": MODEL, "input": "a", "store": False})
        ).json()
        missing = await client.post(
            "/v1/responses",
            json={"model": MODEL, "input": "b", "previous_response_id": hidden["id"]},
        )
        assert missing.status_code == 404


async def test_responses_history_expires_and_is_size_capped(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    async with await responses_client() as client:
        first = (await client.post("/v1/responses", json={"model": MODEL, "input": "a"})).json()
        clock[0] += 1801
        await client.post("/v1/responses", json={"model": MODEL, "input": "b"})
        gone = await client.post(
            "/v1/responses",
            json={"model": MODEL, "input": "c", "previous_response_id": first["id"]},
        )
        assert gone.status_code == 404
    monkeypatch.setattr("m365_agent_gateway.api.HISTORY_CHARS", 300)
    async with await responses_client() as client:
        ids = [
            (await client.post("/v1/responses", json={"model": MODEL, "input": "x" * 100})).json()[
                "id"
            ]
            for _ in range(4)
        ]
        old = await client.post(
            "/v1/responses",
            json={"model": MODEL, "input": "c", "previous_response_id": ids[0]},
        )
        assert old.status_code == 404
        recent = await client.post(
            "/v1/responses",
            json={"model": MODEL, "input": "c", "previous_response_id": ids[-1]},
        )
        assert recent.status_code == 200
