import json

import pytest

from m365_agent_gateway.errors import GatewayError
from m365_agent_gateway.tools import (
    Tool,
    bounded_tree,
    decode_reply,
    extract_envelope,
    render_prompt,
)


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


def test_pretty_printed_envelope_after_prose(tool):
    call = '{"name": "read_file", "arguments": {"path": "a"}}'
    raw = f'I will read it.\n{{\n  "text": "",\n  "calls": [\n    {call}\n  ]\n}}'
    assert decode_reply(raw, [tool]).calls[0].arguments == {"path": "a"}


def test_nested_text_object_in_arguments_is_not_the_envelope():
    schema = {"type": "object", "properties": {"note": {"type": "object"}}}
    note = Tool("note", "", schema)
    call = '{"name": "note", "arguments": {"note": {"text": 1, "calls": 2}}}'
    raw = f'Sure: {{"calls": [{call}], "text": ""}}'
    assert decode_reply(raw, [note]).calls[0].arguments == {"note": {"text": 1, "calls": 2}}
    assert extract_envelope('prose {"text": "a"} more') is None


def test_draft07_tuple_items_and_draft04_boolean_exclusive_minimum():
    tuple_tool = Tool(
        "pair",
        "",
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {
                "p": {"type": "array", "items": [{"type": "string"}, {"type": "integer"}]}
            },
        },
    )
    old = Tool(
        "num",
        "",
        {
            "$schema": "http://json-schema.org/draft-04/schema#",
            "type": "object",
            "properties": {"n": {"type": "number", "minimum": 1, "exclusiveMinimum": True}},
        },
    )
    ok = '{"text":"","calls":[{"name":"pair","arguments":{"p":["a",1]}}]}'
    assert decode_reply(ok, [tuple_tool, old]).calls[0].name == "pair"
    bad = '{"text":"","calls":[{"name":"pair","arguments":{"p":["a","b"]}}]}'
    with pytest.raises(GatewayError):
        decode_reply(bad, [tuple_tool, old])
    edge = '{"text":"","calls":[{"name":"num","arguments":{"n":1}}]}'
    with pytest.raises(GatewayError):
        decode_reply(edge, [tuple_tool, old])
    assert decode_reply(edge.replace('"n":1', '"n":2'), [tuple_tool, old]).calls[0].name == "num"


def test_ref_rule_applies_to_schemas_only():
    bounded_tree({"input": {"$ref": "https://x.test"}})
    with pytest.raises(GatewayError):
        bounded_tree({"$ref": "https://x.test"}, schema=True)
