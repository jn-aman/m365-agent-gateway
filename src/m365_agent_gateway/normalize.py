"""Translate text and client-owned tool history without claiming native model controls."""

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .errors import GatewayError
from .tools import Tool, bounded_tree, reject_constant, unique_object

MODEL_OPTIONS = {
    "m365-agent": ("Auto", "Magic"),
    "m365-agent-quick": ("Quick response", "Gpt_5_5_Chat"),
    "m365-agent-think-deeper": ("Think deeper", "Gpt_5_5_Reasoning"),
    "m365-agent-sonnet": ("Claude Sonnet", "Claude_Sonnet"),
    "m365-agent-opus": ("Claude Opus", "Claude_Opus"),
    "m365-agent-gpt-5.6": ("GPT-5.6 Quick response", "Gpt_5_6_Chat"),
    "m365-agent-gpt-5.6-think-deeper": ("GPT-5.6 Think deeper", "Gpt_5_6_Reasoning"),
}
IMAGE_TYPES = {"image/gif", "image/heic", "image/jpeg", "image/png", "image/webp"}


@dataclass(frozen=True)
class ImageData:
    media_type: str
    data: str


@dataclass
class Turn:
    model: str
    tone: str
    messages: list[dict[str, Any]]
    images: list[ImageData]
    tools: list[Tool]
    choice: str
    stream: bool
    max_chars: int
    parallel: bool


def data_image(value: Any, max_bytes: int) -> ImageData:
    if not isinstance(value, str) or not value.startswith("data:") or "," not in value:
        raise GatewayError("Only base64 image data URLs are supported.", "unsupported_feature")
    header, data = value[5:].split(",", 1)
    media_type, *parameters = header.split(";")
    media_type = media_type.lower()
    if media_type not in IMAGE_TYPES or "base64" not in parameters:
        raise GatewayError("Unsupported image encoding or media type.", "unsupported_feature")
    try:
        decoded = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        raise GatewayError("Invalid base64 image data.") from None
    if not decoded or len(decoded) > max_bytes:
        raise GatewayError("Image exceeds the local request limit.", "request_too_large", 413)
    return ImageData(media_type, data)


def image_block(block: dict[str, Any], protocol: str, max_bytes: int) -> ImageData:
    kind = block.get("type")
    if kind == "image_url":
        source = block.get("image_url")
        value = source.get("url") if isinstance(source, dict) else None
    elif kind == "input_image":
        value = block.get("image_url")
    elif kind == "image" and protocol == "anthropic":
        source = block.get("source")
        if isinstance(source, dict) and source.get("type") == "base64":
            media_type, data = source.get("media_type"), source.get("data")
            value = f"data:{media_type};base64,{data}" if isinstance(data, str) else None
        else:
            value = None
    else:
        raise GatewayError("Unsupported image content block.", "unsupported_feature")
    return data_image(value, max_bytes)


def text_content(
    value: Any,
    images: list[ImageData] | None = None,
    protocol: str = "chat",
    max_bytes: int = 2_000_000,
) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            if not isinstance(block, dict):
                raise GatewayError("Invalid content block.")
            if block.get("type") in {"image_url", "input_image", "image"}:
                if images is None:
                    raise GatewayError(
                        "Images are only supported in message content.", "unsupported_feature"
                    )
                images.append(image_block(block, protocol, max_bytes))
                parts.append("[Image attached]")
                continue
            if block.get("type") not in {"text", "input_text", "output_text"}:
                raise GatewayError("Unsupported content block.", "unsupported_feature")
            if not isinstance(block.get("text"), str):
                raise GatewayError("Content text must be a string.")
            parts.append(block["text"])
        return "\n".join(parts)
    raise GatewayError("Content must be text or text blocks.")


def arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(
                value, object_pairs_hook=unique_object, parse_constant=reject_constant
            )
        except (ValueError, RecursionError):
            raise GatewayError("Invalid historical tool arguments.") from None
    if not isinstance(value, dict):
        raise GatewayError("Tool arguments must be an object.")
    bounded_tree(value)
    return value


def normalize(
    body: dict[str, Any],
    protocol: str,
    settings: Settings,
    history: list[dict[str, Any]] | None = None,
) -> Turn:
    try:
        return _normalize(body, protocol, settings, history)
    except (AttributeError, TypeError, KeyError, ValueError, RecursionError):
        raise GatewayError("Invalid request field types.") from None


def _normalize(
    body: dict[str, Any],
    protocol: str,
    settings: Settings,
    history: list[dict[str, Any]] | None = None,
) -> Turn:
    model = body.get("model", settings.model)
    if not isinstance(model, str) or model not in MODEL_OPTIONS:
        raise GatewayError("Unknown model; select a model from /v1/models.")
    tone = MODEL_OPTIONS[model][1]
    if body.get("n", 1) != 1:
        raise GatewayError("Only one completion is supported.", "unsupported_feature")
    thinking = body.get("thinking", {}).get("type", "disabled")
    # Upstream has no thinking switch; tone decides, so the request flag is advisory.
    if thinking not in {"disabled", "enabled", "adaptive"}:
        raise GatewayError("Unsupported thinking mode.", "unsupported_feature")
    if body.get("response_format", {}).get("type", "text") != "text":
        raise GatewayError("Constrained output formats are not supported.", "unsupported_feature")
    tools = []
    for item in body.get("tools", []):
        if not isinstance(item, dict):
            raise GatewayError("Invalid tool definition.")
        if protocol == "anthropic":
            name = item.get("name")
            schema = item.get("input_schema", {"type": "object"})
            definition = item
        else:
            if item.get("type") != "function":
                raise GatewayError(
                    "Only client-executed function tools are supported.", "unsupported_feature"
                )
            definition = item.get("function", item)
            name = definition.get("name")
            schema = definition.get("parameters", {"type": "object"})
        if not isinstance(name, str) or not isinstance(schema, dict):
            raise GatewayError("Invalid tool name or parameters.")
        tools.append(Tool(name, str(definition.get("description", "")), schema))
    if len(tools) > 128 or len({tool.name for tool in tools}) != len(tools):
        raise GatewayError("Tools must be unique and limited to 128.")
    if tools and not settings.emulate_tools:
        raise GatewayError("Experimental tool emulation is disabled.", "unsupported_feature")
    choice = body.get("tool_choice", "auto")
    if isinstance(choice, dict):
        if choice.get("type") == "auto":
            choice = "auto"
        elif choice.get("type") in {"none", "any"}:
            choice = "none" if choice["type"] == "none" else "required"
        else:
            choice = choice.get("function", choice).get("name")
    if not isinstance(choice, str):
        raise GatewayError("Invalid tool choice.")
    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise GatewayError("stream must be boolean.")
    budget = body.get("max_tokens", body.get("max_output_tokens", settings.max_output // 4))
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        raise GatewayError("Output budget must be a positive integer.")
    messages = list(history or [])
    images: list[ImageData] = []
    instructions = body.get("system", body.get("instructions"))
    if instructions is not None:
        messages.append({"role": "system", "content": text_content(instructions)})
    incoming = body.get("input") if protocol == "responses" else body.get("messages")
    if isinstance(incoming, str) and protocol == "responses":
        incoming = [{"role": "user", "content": incoming}]
    if not isinstance(incoming, list) or not incoming or len(incoming) > 512:
        raise GatewayError("Supply 1 to 512 messages/input items.")
    for message in incoming:
        if not isinstance(message, dict):
            raise GatewayError("Messages must be objects.")
        kind = message.get("type")
        if protocol == "responses" and kind == "function_call":
            messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": message.get("call_id"),
                            "name": message.get("name"),
                            "arguments": arguments(message.get("arguments", "{}")),
                        }
                    ],
                }
            )
            continue
        if protocol == "responses" and kind == "function_call_output":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": message.get("call_id"),
                    "content": text_content(message.get("output")),
                }
            )
            continue
        role = message.get("role")
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            raise GatewayError("Unsupported message role or input item.", "unsupported_feature")
        content = message.get("content")
        if protocol == "anthropic" and isinstance(content, list):
            text_blocks, calls = [], []
            for block in content:
                if not isinstance(block, dict):
                    raise GatewayError("Invalid content block.")
                if block.get("type") == "tool_use":
                    calls.append(
                        {
                            "id": block.get("id"),
                            "name": block.get("name"),
                            "arguments": arguments(block.get("input", {})),
                        }
                    )
                elif block.get("type") == "tool_result":
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id"),
                            "content": text_content(block.get("content")),
                            "is_error": block.get("is_error", False),
                        }
                    )
                elif block.get("type") == "image":
                    images.append(image_block(block, protocol, settings.max_body))
                    text_blocks.append({"type": "text", "text": "[Image attached]"})
                else:
                    text_blocks.append(block)
            entry = {
                "role": role,
                "content": text_content(text_blocks, images, protocol, settings.max_body),
            }
            if calls:
                entry["tool_calls"] = calls
            if entry["content"] or calls:
                messages.append(entry)
        else:
            entry = {
                "role": role,
                "content": text_content(content, images, protocol, settings.max_body),
            }
            if role == "tool":
                entry["tool_call_id"] = message.get("tool_call_id")
            if message.get("tool_calls"):
                entry["tool_calls"] = [
                    {
                        "id": call.get("id"),
                        "name": call.get("function", {}).get("name"),
                        "arguments": arguments(call.get("function", {}).get("arguments", "{}")),
                    }
                    for call in message["tool_calls"]
                ]
            messages.append(entry)
    if len(json.dumps(messages)) > settings.max_prompt:
        raise GatewayError(
            "Conversation exceeds local context limit; start a fresh session.", "context_limit", 413
        )
    parallel = body.get("parallel_tool_calls", True)
    if isinstance(body.get("tool_choice"), dict):
        parallel = parallel and not body["tool_choice"].get("disable_parallel_tool_use", False)
    return Turn(
        model,
        tone,
        messages,
        images,
        tools,
        choice,
        stream,
        min(budget * 4, settings.max_output),
        bool(parallel),
    )
