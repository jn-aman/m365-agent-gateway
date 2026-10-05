"""Single bounded generation path shared by all client protocols."""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol

import anyio

from .config import Settings
from .errors import GatewayError
from .normalize import ImageData, Turn
from .tools import Reply, decode_reply, render_prompt
from .upstream import MAX_FRAME_BYTES


class Upstream(Protocol):
    def stream(
        self, prompt: str, tone: str, images: Sequence[ImageData] = ()
    ) -> AsyncIterator[str]: ...


@dataclass
class Result:
    reply: Reply
    limited: bool = False


def prepare(turn: Turn, settings: Settings) -> str:
    prompt = render_prompt(turn.messages, turn.tools, turn.choice, len(turn.images))
    if len(prompt) > settings.max_prompt:
        raise GatewayError(
            "Prompt plus tool definitions exceeds context limit.", "context_limit", 413
        )
    # The escaped prompt alone is a lower bound of the frame, so this never rejects a valid one.
    if len(json.dumps(prompt)) > MAX_FRAME_BYTES:
        raise GatewayError("Prompt exceeds Copilot's ~2 MB request limit.", "context_limit", 413)
    return prompt


async def generate(
    turn: Turn, upstream: Upstream, settings: Settings, prompt: str | None = None
) -> AsyncIterator[str | Result]:
    if prompt is None:
        prompt = prepare(turn, settings)
    buffered = bool(turn.tools and turn.choice != "none")
    output = ""
    iterator = upstream.stream(prompt, turn.tone, turn.images)
    limited = False
    deadline = asyncio.get_running_loop().time() + settings.timeout
    try:
        while True:
            try:
                async with asyncio.timeout_at(deadline):
                    piece = await anext(iterator)
            except StopAsyncIteration:
                break
            if not isinstance(piece, str):
                raise GatewayError("Invalid upstream text.", "upstream_protocol", 502)
            if len(output) + len(piece) > settings.max_output:
                raise GatewayError("Upstream output exceeds limit.", "output_limit", 502)
            if not buffered and len(output) + len(piece) > turn.max_chars:
                piece = piece[: max(0, turn.max_chars - len(output))]
                limited = True
            output += piece
            if not buffered and piece:
                yield piece
            if limited:
                break
        await iterator.aclose()
        reply = decode_reply(output, turn.tools, turn.choice)
        if not turn.parallel and len(reply.calls) > 1:
            raise GatewayError(
                "Multiple calls returned with parallel tools disabled.", "tool_protocol", 502
            )
        if buffered and len(output) > turn.max_chars:
            raise GatewayError(
                "Tool response exceeds requested output budget.", "output_limit", 502
            )
        yield Result(reply, limited)
    except TimeoutError:
        raise GatewayError("Generation timed out.", "upstream_timeout", 504) from None
    finally:
        with anyio.CancelScope(shield=True):
            await iterator.aclose()
