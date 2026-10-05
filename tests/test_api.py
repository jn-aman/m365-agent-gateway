import json

import httpx
import pytest

from m365_agent_gateway.api import create_app
from m365_agent_gateway.config import Settings

MODEL = "m365-agent"
MODEL_TONES = {
    "m365-agent": "Magic",
    "m365-agent-quick": "Gpt_5_5_Chat",
    "m365-agent-think-deeper": "Gpt_5_5_Reasoning",
    "m365-agent-sonnet": "Claude_Sonnet",
    "m365-agent-opus": "Claude_Opus",
    "m365-agent-gpt-5.6": "Gpt_5_6_Chat",
    "m365-agent-gpt-5.6-think-deeper": "Gpt_5_6_Reasoning",
}


class FakeUpstream:
    def __init__(self, parts=None):
        self.parts = parts or ["Hello", " world"]
        self.prompts = []
        self.tones = []
        self.images = []

    async def stream(self, prompt, tone, images=()):
        self.prompts.append(prompt)
        self.tones.append(tone)
        self.images.append(list(images))
        for part in self.parts:
            yield part


@pytest.fixture
async def client():
    upstream = FakeUpstream()
    app = create_app(Settings(), upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        yield client, upstream


async def test_local_api_needs_no_key_and_lists_supported_models(client):
    client, _ = client
    assert (await client.get("/health")).status_code == 200
    response = await client.get("/v1/models")
    assert response.status_code == 200
    assert {item["id"] for item in response.json()["data"]} == set(MODEL_TONES)


async def test_openai_chat_and_stream(client):
    client, upstream = client
    body = {"model": MODEL, "messages": [{"role": "user", "content": "hello"}]}
    response = await client.post("/v1/chat/completions", json=body)
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "Hello world"
    response = await client.post("/v1/chat/completions", json={**body, "stream": True})
    assert '"content":"Hello"' in response.text
    assert "data: [DONE]" in response.text
    assert "hello" in upstream.prompts[0]


async def test_anthropic_message_and_stream(client):
    client, _ = client
    headers = {"anthropic-version": "2023-06-01"}
    body = {"model": MODEL, "max_tokens": 100, "messages": [{"role": "user", "content": "hello"}]}
    response = await client.post("/v1/messages", headers=headers, json=body)
    assert response.json()["content"][0]["text"] == "Hello world"
    response = await client.post("/v1/messages", headers=headers, json={**body, "stream": True})
    assert "event: message_start" in response.text
    assert "event: content_block_delta" in response.text
    assert "event: message_stop" in response.text


async def test_responses_and_previous_context(client):
    client, upstream = client
    response = await client.post(
        "/v1/responses",
        json={
            "model": MODEL,
            "input": "first",
            "store": True,
        },
    )
    value = response.json()
    assert value["output"][0]["content"][0]["text"] == "Hello world"
    response = await client.post(
        "/v1/responses",
        json={
            "model": MODEL,
            "input": "second",
            "previous_response_id": value["id"],
            "stream": True,
        },
    )
    assert "response.output_text.delta" in response.text
    assert "response.completed" in response.text
    assert "first" in upstream.prompts[-1] and "second" in upstream.prompts[-1]


async def test_undeclared_model_and_images_rejected(client):
    client, _ = client
    body = {"model": "pretend-claude", "messages": [{"role": "user", "content": "hi"}]}
    assert (await client.post("/v1/chat/completions", json=body)).status_code == 400
    body["model"] = MODEL
    body["messages"][0]["content"] = [{"type": "image_url", "image_url": {"url": "file:///secret"}}]
    assert (await client.post("/v1/chat/completions", json=body)).status_code == 400


@pytest.mark.parametrize(("model", "tone"), MODEL_TONES.items())
async def test_selected_model_sets_upstream_tone_and_response_model(client, model, tone):
    client, upstream = client
    response = await client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200
    assert response.json()["model"] == model
    assert upstream.tones[-1] == tone


@pytest.mark.parametrize(
    ("protocol", "body"),
    [
        (
            "chat",
            {
                "model": MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Describe this"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
                            },
                        ],
                    }
                ],
            },
        ),
        (
            "responses",
            {
                "model": MODEL,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Describe this"},
                            {"type": "input_image", "image_url": "data:image/png;base64,aW1hZ2U="},
                        ],
                    }
                ],
            },
        ),
        (
            "anthropic",
            {
                "model": MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Describe this"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "aW1hZ2U=",
                                },
                            },
                        ],
                    }
                ],
            },
        ),
    ],
)
def test_image_input_normalizes_for_each_protocol(protocol, body):
    from m365_agent_gateway.normalize import normalize

    turn = normalize(body, protocol, Settings())
    assert len(turn.images) == 1
    assert turn.images[0].media_type == "image/png"
    assert turn.images[0].data == "aW1hZ2U="
    assert "[Image attached]" in turn.messages[-1]["content"]


def test_remote_image_url_is_rejected(client):

    from m365_agent_gateway.errors import GatewayError
    from m365_agent_gateway.normalize import normalize

    with pytest.raises(GatewayError, match="base64 image data URLs"):
        normalize(
            {
                "model": MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.test/private.png"},
                            }
                        ],
                    }
                ],
            },
            "chat",
            Settings(),
        )


async def test_thinking_flag_is_accepted_and_tone_decides(client):
    client, upstream = client
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m365-agent-think-deeper",
            "thinking": {"type": "enabled", "budget_tokens": 1000},
            "messages": [{"role": "user", "content": "reason"}],
        },
    )
    assert response.status_code == 200
    assert upstream.tones[-1] == "Gpt_5_5_Reasoning"
    for mode in ("enabled", "adaptive"):
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "thinking": {"type": mode},
                "messages": [{"role": "user", "content": "reason"}],
            },
        )
        assert response.status_code == 200
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL,
            "thinking": {"type": "bogus"},
            "messages": [{"role": "user", "content": "reason"}],
        },
    )
    assert response.status_code == 400


async def test_image_reaches_upstream_and_is_not_embedded_in_prompt(client):
    client, upstream = client
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
                        },
                    ],
                }
            ],
        },
    )
    assert response.status_code == 200
    assert "[Image attached]" in upstream.prompts[-1]
    assert len(upstream.images[-1]) == 1
    assert upstream.images[-1][0].data == "aW1hZ2U="


async def test_regex_tool_schema_is_accepted(client):
    client, upstream = client
    upstream.parts = ['{"text":"ok","calls":[]}']
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "read"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "parameters": {
                            "$id": "https://schemas.example.test/read-file",
                            "type": "object",
                            "properties": {"path": {"type": "string", "pattern": "^/"}},
                            "patternProperties": {"^x-[a-z]+$": {"type": "integer"}},
                        },
                    },
                }
            ],
        },
    )
    assert response.status_code == 200


async def test_untrusted_browser_origin_and_body_limits(client):
    client, upstream = client
    assert (
        await client.get("/v1/models", headers={"Origin": "https://evil.test"})
    ).status_code == 403
    response = await client.post("/v1/chat/completions", content="x" * (Settings().max_body + 1))
    assert response.status_code == 413
    assert not upstream.prompts


async def test_parallel_tool_calls_translated():
    upstream = FakeUpstream(
        [
            json.dumps(
                {
                    "text": "",
                    "calls": [
                        {"name": "read_file", "arguments": {"path": "a"}},
                        {"name": "read_file", "arguments": {"path": "b"}},
                    ],
                }
            )
        ]
    )
    app = create_app(Settings(), upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        tool = {
            "type": "function",
            "function": {
                "name": "read_file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "read"}],
                "tools": [tool],
            },
        )
        assert response.json()["choices"][0]["finish_reason"] == "tool_calls"
        assert len(response.json()["choices"][0]["message"]["tool_calls"]) == 2
        response = await client.post(
            "/v1/messages",
            json={
                "model": MODEL,
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "read"}],
                "tools": [{"name": "read_file", "input_schema": tool["function"]["parameters"]}],
                "stream": True,
            },
        )
        assert "input_json_delta" in response.text
        assert '"stop_reason":"tool_use"' in response.text


async def test_tool_results_are_preserved(client):
    client, upstream = client
    response = await client.post(
        "/v1/messages",
        json={
            "model": MODEL,
            "max_tokens": 100,
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "call_a", "name": "read", "input": {}}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_a",
                            "content": "denied",
                            "is_error": True,
                        }
                    ],
                },
            ],
        },
    )
    assert response.status_code == 200
    assert "denied" in upstream.prompts[-1] and "call_a" in upstream.prompts[-1]
