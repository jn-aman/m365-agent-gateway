"""Authenticated localhost-only HTTP compatibility service."""

import asyncio
import json
import logging
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Settings
from .errors import GatewayError
from .normalize import MODEL_OPTIONS, normalize
from .protocols import WireStream, complete, sse
from .service import Result, Upstream, generate
from .tools import bounded_tree, reject_constant, unique_object

logger = logging.getLogger("uvicorn.error")


def error_body(error: GatewayError, anthropic: bool) -> dict[str, Any]:
    if anthropic and error.code == "context_limit":
        # Claude Code auto-compacts only on Anthropic's own too-long wording.
        return {"type": "invalid_request_error", "message": f"prompt is too long: {error}"}
    return {"type": error.code, "message": str(error)}


def error_response(error: GatewayError, anthropic: bool = False) -> JSONResponse:
    too_long = anthropic and error.code == "context_limit"
    return JSONResponse(
        status_code=400 if too_long else error.status,
        content={
            **({"type": "error"} if anthropic else {}),
            "error": error_body(error, anthropic),
        },
    )


class LocalGuard:
    def __init__(self, app: Any, settings: Settings):
        self.app, self.settings = app, settings
        self.requests: deque[float] = deque()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        host = headers.get(b"host", b"").decode("latin1")
        try:
            valid_host = urlsplit("http://" + host).hostname in {"127.0.0.1", "localhost", "::1"}
        except ValueError:
            valid_host = False
        failure = None
        if not valid_host or b"origin" in headers:
            failure = GatewayError(
                "Only local non-browser clients are allowed.", "access_denied", 403
            )
        if failure is not None:
            await error_response(failure, scope["path"].startswith("/v1/messages"))(
                scope, receive, send
            )
            return
        now = time.monotonic()
        while self.requests and self.requests[0] < now - 60:
            self.requests.popleft()
        if scope["path"] != "/health":
            if len(self.requests) >= self.settings.rate_per_minute:
                await error_response(GatewayError("Local rate limit exceeded.", "rate_limit", 429))(
                    scope, receive, send
                )
                return
            self.requests.append(now)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.settings.max_body:
                await error_response(
                    GatewayError("Request body too large.", "request_too_large", 413)
                )(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay() -> dict[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(settings: Settings, upstream: Upstream) -> FastAPI:
    app = FastAPI(title="M365 Agent Gateway", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LocalGuard, settings=settings)
    history: OrderedDict[str, tuple[float, list[dict[str, Any]]]] = OrderedDict()

    @app.exception_handler(GatewayError)
    async def handle_error(request: Request, error: GatewayError) -> JSONResponse:
        logger.warning("%s failed: %s (%s)", request.url.path, error.code, error)
        return error_response(error, request.url.path.startswith("/v1/messages"))

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": "0.1.0", "session_verified": False}

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": model,
                    "object": "model",
                    "created": 0,
                    "owned_by": "microsoft",
                    "type": "model",
                    "display_name": name,
                }
                for model, (name, _) in MODEL_OPTIONS.items()
            ],
            "has_more": False,
            "first_id": next(iter(MODEL_OPTIONS)),
            "last_id": next(reversed(MODEL_OPTIONS)),
        }

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request) -> dict[str, Any]:
        body = await read_body(request)
        turn = normalize(body, "anthropic", settings)
        return {
            "input_tokens": (
                len(json.dumps(turn.messages)) + len(json.dumps(body.get("tools", []))) + 3
            )
            // 4,
            "estimated": True,
        }

    async def read_body(request: Request) -> dict[str, Any]:
        try:
            body = json.loads(
                await request.body(),
                object_pairs_hook=unique_object,
                parse_constant=reject_constant,
            )
            if not isinstance(body, dict):
                raise ValueError
            bounded_tree(body)
            return body
        except (ValueError, RecursionError):
            raise GatewayError("Body must be a valid JSON object.") from None

    async def answer(request: Request, protocol: str) -> Any:
        body = await read_body(request)
        prior = None
        if protocol == "responses" and body.get("previous_response_id"):
            old = history.get(body["previous_response_id"])
            if old is None or old[0] < time.monotonic() - 1800:
                raise GatewayError(
                    "Previous response unavailable; send full input history.",
                    "history_missing",
                    404,
                )
            prior = old[1]
        try:
            turn = normalize(body, protocol, settings, prior)
        except (AttributeError, TypeError, KeyError):
            raise GatewayError("Invalid request field types.") from None
        request_id = {"chat": "chatcmpl_", "anthropic": "msg_", "responses": "resp_"}[
            protocol
        ] + uuid.uuid4().hex
        input_text = json.dumps(turn.messages)

        def remember(result: Result) -> None:
            if protocol != "responses" or body.get("store") is not True:
                return
            history[request_id] = (
                time.monotonic(),
                [
                    *turn.messages,
                    {
                        "role": "assistant",
                        "content": result.reply.text,
                        "tool_calls": [
                            {"id": call.id, "name": call.name, "arguments": call.arguments}
                            for call in result.reply.calls
                        ],
                    },
                ],
            )
            while len(history) > 64:
                history.popitem(last=False)

        if not turn.stream:
            result = None
            async for item in generate(turn, upstream, settings):
                if isinstance(item, Result):
                    result = item
            if result is None:
                raise GatewayError("No generation result.", "upstream_protocol", 502)
            remember(result)
            return complete(result, protocol, request_id, turn.model, input_text)

        async def stream() -> AsyncIterator[str]:
            wire = WireStream(protocol, request_id, turn.model)
            iterator = generate(turn, upstream, settings)
            pending = None
            try:
                for event in wire.start():
                    yield event
                while True:
                    if pending is None:
                        pending = asyncio.ensure_future(anext(iterator))
                    done, _ = await asyncio.wait({pending}, timeout=10)
                    if not done:
                        yield (
                            sse({"type": "ping"}, "ping")
                            if protocol == "anthropic"
                            else ": ping\n\n"
                        )
                        continue
                    try:
                        item = pending.result()
                    except StopAsyncIteration:
                        break
                    finally:
                        pending = None
                    if isinstance(item, Result):
                        remember(item)
                        for event in wire.end(item, input_text):
                            yield event
                    else:
                        for event in wire.delta(item):
                            yield event
            except GatewayError as error:
                logger.warning("%s stream failed: %s (%s)", protocol, error.code, error)
                if protocol == "chat":
                    message = f"Gateway error ({error.code}): {error}"
                    yield wire.chat_chunk({"content": message}, "stop")
                    yield "data: [DONE]\n\n"
                else:
                    value = {"type": "error", "error": error_body(error, protocol == "anthropic")}
                    yield sse(value, "error")
            except Exception:
                logger.exception("%s stream failed unexpectedly", protocol)
                message = "Generation failed; check local status."
                if protocol == "chat":
                    yield wire.chat_chunk({"content": f"Gateway error: {message}"}, "stop")
                    yield "data: [DONE]\n\n"
                else:
                    value = {
                        "type": "error",
                        "error": {"type": "gateway_error", "message": message},
                    }
                    yield sse(value, "error")
            finally:
                with anyio.CancelScope(shield=True):
                    if pending is not None:
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                    await iterator.aclose()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-M365-Tool-Emulation": "experimental",
                "X-M365-Usage": "estimated",
            },
        )

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> Any:
        return await answer(request, "chat")

    @app.post("/v1/messages")
    async def messages(request: Request) -> Any:
        return await answer(request, "anthropic")

    @app.post("/v1/responses")
    async def responses(request: Request) -> Any:
        return await answer(request, "responses")

    return app
