import json

import pytest

from m365_agent_gateway.errors import GatewayError
from m365_agent_gateway.tools import Tool, decode_reply, render_prompt


@pytest.fixture
def tool():
    return Tool(
        "read_file",
        "Read an approved file",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )


def test_valid_tool_call(tool):
    reply = decode_reply(
        json.dumps(
            {
                "text": "",
                "calls": [
                    {"name": "read_file", "arguments": {"path": "example.txt"}},
                ],
            }
        ),
        [tool],
    )
    assert reply.calls[0].name == "read_file"
    assert reply.calls[0].arguments == {"path": "example.txt"}
    assert reply.calls[0].id.startswith("call_")


@pytest.mark.parametrize(
    "raw",
    [
        '{"text":"","calls":[{"name":"exec","arguments":{}}]}',
        '{"text":"","calls":[{"name":"read_file","arguments":{"path":42}}]}',
        '{"text":"","calls":[{"name":"read_file","arguments":{"path":"x","extra":1}}]}',
        '{"text":"","calls": "bad"}',
        '{"text":"","calls": [',
    ],
)
def test_invalid_tool_reply_fails_closed(raw, tool):
    with pytest.raises(GatewayError):
        decode_reply(raw, [tool])


def test_prose_reply_is_text_only_in_auto_mode(tool):
    reply = decode_reply("Hi! How can I help?", [tool])
    assert reply.text == "Hi! How can I help?" and reply.calls == []
    with pytest.raises(GatewayError):
        decode_reply("Hi!", [tool], "required")


def test_envelope_inside_fence_or_preamble(tool):
    envelope = '{"text":"","calls":[{"name":"read_file","arguments":{"path":"a"}}]}'
    for raw in (f"```\n{envelope}\n```", f"```json {envelope}```", f"Sure:\n{envelope}"):
        assert decode_reply(raw, [tool]).calls[0].arguments == {"path": "a"}


def test_plain_text_is_not_interpreted_as_tool_call():
    raw = '{"text":"secret", "calls": [{"name":"shell"}]}'
    assert decode_reply(raw, []).text == raw


def test_required_and_named_tool_choice(tool):
    with pytest.raises(GatewayError):
        decode_reply('{"text":"No", "calls":[]}', [tool], "required")
    with pytest.raises(GatewayError):
        render_prompt([{"role": "user", "content": "hi"}], [tool], "undeclared")


def test_schema_refs_are_local_only():
    with pytest.raises(GatewayError):
        Tool("bad", "", {"$ref": "https://example.com/schema"})


def test_schema_identifier_and_regex_constraints_are_supported():
    tool = Tool(
        "validate",
        "",
        {
            "$id": "https://schemas.example.test/validate",
            "type": "object",
            "properties": {"code": {"type": "string", "pattern": "^[A-Z]{2}-[0-9]{3}$"}},
            "patternProperties": {"^x-[a-z]+$": {"type": "integer"}},
            "additionalProperties": False,
        },
    )
    reply = decode_reply(
        '{"text":"","calls":[{"name":"validate","arguments":{"code":"AB-123","x-count":2}}]}',
        [tool],
    )
    assert reply.calls[0].arguments == {"code": "AB-123", "x-count": 2}
    with pytest.raises(GatewayError):
        decode_reply(
            '{"text":"","calls":[{"name":"validate","arguments":{"code":"bad","x-count":2}}]}',
            [tool],
        )
    with pytest.raises(GatewayError):
        decode_reply(
            '{"text":"","calls":[{"name":"validate","arguments":{"code":"AB-123","x-count":"two"}}]}',
            [tool],
        )


def test_regex_validation_times_out_and_fails_closed():
    tool = Tool(
        "validate",
        "",
        {"type": "object", "properties": {"value": {"type": "string", "pattern": "^(a+)+$"}}},
    )
    with pytest.raises(GatewayError, match="violate schema"):
        decode_reply(
            '{"text":"","calls":[{"name":"validate","arguments":{"value":"'
            + "a" * 10_000
            + '!"}}]}',
            [tool],
        )


def test_history_is_serialized_without_executing_content(tool):
    prompt = render_prompt(
        [
            {"role": "system", "content": "Use tools."},
            {"role": "user", "content": "Inspect file."},
            {"role": "assistant", "tool_calls": [{"id": "call_1", "name": "read_file"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "permission denied"},
        ],
        [tool],
    )
    assert "permission denied" in prompt
    assert "call_1" in prompt
