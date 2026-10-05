"""Wire envelopes and server-sent events for supported client protocols."""

import json
import time
from typing import Any

from .service import Result


def sse(value: Any, event: str | None = None) -> str:
    data = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return (f"event: {event}\n" if event else "") + f"data: {data}\n\n"


def usage(prompt: str, text: str) -> dict[str, int]:
    return {"input_tokens": (len(prompt) + 3) // 4, "output_tokens": (len(text) + 3) // 4}


def chat_message(result: Result) -> dict[str, Any]:
    reply = result.reply
    message: dict[str, Any] = {"role": "assistant", "content": reply.text or None}
    if reply.calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, separators=(",", ":")),
                },
            }
            for call in reply.calls
        ]
    return message


def anthropic_blocks(result: Result) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if result.reply.text:
        blocks.append({"type": "text", "text": result.reply.text})
    blocks.extend(
        {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
        for call in result.reply.calls
    )
    return blocks or [{"type": "text", "text": ""}]


def response_items(result: Result, response_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if result.reply.text:
        items.append(
            {
                "id": "msg_" + response_id[5:],
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": result.reply.text, "annotations": []}],
            }
        )
    for call in result.reply.calls:
        items.append(
            {
                "id": "fc_" + call.id[5:],
                "type": "function_call",
                "status": "completed",
                "call_id": call.id,
                "name": call.name,
                "arguments": json.dumps(call.arguments, separators=(",", ":")),
            }
        )
    return items


def finish(result: Result, protocol: str) -> str:
    if result.limited:
        return "max_tokens" if protocol == "anthropic" else "length"
    if result.reply.calls:
        return "tool_use" if protocol == "anthropic" else "tool_calls"
    return "end_turn" if protocol == "anthropic" else "stop"


def complete(
    result: Result, protocol: str, request_id: str, model: str, input_text: str
) -> dict[str, Any]:
    counts = usage(
        input_text, result.reply.text + json.dumps([call.arguments for call in result.reply.calls])
    )
    if protocol == "chat":
        return {
            "id": request_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": chat_message(result),
                    "finish_reason": finish(result, protocol),
                }
            ],
            "usage": {
                "prompt_tokens": counts["input_tokens"],
                "completion_tokens": counts["output_tokens"],
                "total_tokens": sum(counts.values()),
            },
            "usage_estimated": True,
        }
    if protocol == "anthropic":
        return {
            "id": request_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": anthropic_blocks(result),
            "stop_reason": finish(result, protocol),
            "stop_sequence": None,
            "usage": counts,
        }
    return {
        "id": request_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "incomplete" if result.limited else "completed",
        "model": model,
        "output": response_items(result, request_id),
        "error": None,
        "incomplete_details": {"reason": "max_output_tokens"} if result.limited else None,
        "usage": {**counts, "total_tokens": sum(counts.values())},
        "metadata": {"usage_estimated": "true", "tool_emulation": "experimental"},
    }


class WireStream:
    def __init__(self, protocol: str, request_id: str, model: str):
        self.protocol, self.id, self.model = protocol, request_id, model
        self.text = ""
        self.started_text = False
        self.sequence = 0

    def response_event(self, kind: str, **fields: Any) -> str:
        event = sse({"type": kind, "sequence_number": self.sequence, **fields}, kind)
        self.sequence += 1
        return event

    def chat_chunk(self, delta: dict[str, Any], reason: str | None = None) -> str:
        return sse(
            {
                "id": self.id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": self.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
            }
        )

    def start(self) -> list[str]:
        if self.protocol == "chat":
            return [self.chat_chunk({"role": "assistant"})]
        if self.protocol == "anthropic":
            return [
                sse(
                    {
                        "type": "message_start",
                        "message": {
                            "id": self.id,
                            "type": "message",
                            "role": "assistant",
                            "model": self.model,
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0},
                        },
                    },
                    "message_start",
                )
            ]
        stub = {
            "id": self.id,
            "object": "response",
            "status": "in_progress",
            "model": self.model,
            "created_at": int(time.time()),
            "output": [],
        }
        return [
            self.response_event("response.created", response=stub),
            self.response_event("response.in_progress", response=stub),
        ]

    def delta(self, text: str) -> list[str]:
        events = []
        if not self.started_text:
            self.started_text = True
            if self.protocol == "anthropic":
                events.append(
                    sse(
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "text", "text": ""},
                        },
                        "content_block_start",
                    )
                )
            elif self.protocol == "responses":
                item = {
                    "id": "msg_" + self.id[5:],
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                }
                events += [
                    self.response_event("response.output_item.added", output_index=0, item=item),
                    self.response_event(
                        "response.content_part.added",
                        output_index=0,
                        item_id=item["id"],
                        content_index=0,
                        part={"type": "output_text", "text": "", "annotations": []},
                    ),
                ]
        self.text += text
        if self.protocol == "chat":
            events.append(self.chat_chunk({"content": text}))
        elif self.protocol == "anthropic":
            events.append(
                sse(
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text},
                    },
                    "content_block_delta",
                )
            )
        else:
            events.append(
                self.response_event(
                    "response.output_text.delta",
                    output_index=0,
                    item_id="msg_" + self.id[5:],
                    content_index=0,
                    delta=text,
                )
            )
        return events

    def end(self, result: Result, input_text: str) -> list[str]:
        events = []
        if result.reply.text and not self.started_text:
            events.extend(self.delta(result.reply.text))
        if self.protocol == "chat":
            for index, call in enumerate(result.reply.calls):
                events.append(
                    self.chat_chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "id": call.id,
                                    "type": "function",
                                    "function": {
                                        "name": call.name,
                                        "arguments": json.dumps(
                                            call.arguments, separators=(",", ":")
                                        ),
                                    },
                                }
                            ]
                        }
                    )
                )
            return events + [self.chat_chunk({}, finish(result, "chat")), "data: [DONE]\n\n"]
        if self.protocol == "anthropic":
            offset = int(self.started_text)
            if self.started_text:
                events.append(sse({"type": "content_block_stop", "index": 0}, "content_block_stop"))
            for index, call in enumerate(result.reply.calls, offset):
                events.extend(
                    [
                        sse(
                            {
                                "type": "content_block_start",
                                "index": index,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": call.id,
                                    "name": call.name,
                                    "input": {},
                                },
                            },
                            "content_block_start",
                        ),
                        sse(
                            {
                                "type": "content_block_delta",
                                "index": index,
                                "delta": {
                                    "type": "input_json_delta",
                                    "partial_json": json.dumps(call.arguments),
                                },
                            },
                            "content_block_delta",
                        ),
                        sse({"type": "content_block_stop", "index": index}, "content_block_stop"),
                    ]
                )
            counts = usage(
                input_text,
                result.reply.text + json.dumps([call.arguments for call in result.reply.calls]),
            )
            return events + [
                sse(
                    {
                        "type": "message_delta",
                        "delta": {
                            "stop_reason": finish(result, "anthropic"),
                            "stop_sequence": None,
                        },
                        "usage": counts,
                    },
                    "message_delta",
                ),
                sse({"type": "message_stop"}, "message_stop"),
            ]
        items = response_items(result, self.id)
        if self.started_text:
            item = items[0]
            events.extend(
                [
                    self.response_event(
                        "response.output_text.done",
                        output_index=0,
                        item_id=item["id"],
                        content_index=0,
                        text=self.text,
                    ),
                    self.response_event(
                        "response.content_part.done",
                        output_index=0,
                        item_id=item["id"],
                        content_index=0,
                        part=item["content"][0],
                    ),
                    self.response_event("response.output_item.done", output_index=0, item=item),
                ]
            )
        for index, item in enumerate(items):
            if item["type"] != "function_call":
                continue
            events.extend(
                [
                    self.response_event(
                        "response.output_item.added",
                        output_index=index,
                        item={**item, "status": "in_progress", "arguments": ""},
                    ),
                    self.response_event(
                        "response.function_call_arguments.delta",
                        output_index=index,
                        item_id=item["id"],
                        delta=item["arguments"],
                    ),
                    self.response_event(
                        "response.function_call_arguments.done",
                        output_index=index,
                        item_id=item["id"],
                        arguments=item["arguments"],
                    ),
                    self.response_event("response.output_item.done", output_index=index, item=item),
                ]
            )
        envelope = complete(result, "responses", self.id, self.model, input_text)
        kind = "response.incomplete" if result.limited else "response.completed"
        return events + [self.response_event(kind, response=envelope)]
