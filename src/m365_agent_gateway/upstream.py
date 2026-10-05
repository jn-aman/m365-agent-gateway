"""Bounded, isolated streaming over the signed-in Microsoft web transport."""

import asyncio
import copy
import json
import logging
import ssl
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import anyio
import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .config import Settings
from .errors import GatewayError
from .normalize import ImageData
from .sessions import SecretStore, Session

SEPARATOR = "\x1e"


def decode_frames(payload: str) -> list[dict[str, Any]]:
    try:
        frames = [json.loads(part) for part in payload.split(SEPARATOR) if part.strip()]
        if any(not isinstance(frame, dict) for frame in frames):
            raise ValueError
        return frames
    except (ValueError, RecursionError):
        raise GatewayError("Invalid upstream frame.", "upstream_protocol", 502) from None


UPLOAD_OPTIONS = (
    "cwcgptvsan",
    "flux_v3_gptv_enable_upload_multi_image_in_turn_wo_ch",
    "gptvnorm2048",
)
MAX_FRAME_BYTES = 2_000_000
VISION_CHAT_OPTIONS = (
    "flux_v3_gptv_enable_upload_multi_image_in_turn_wo_ch",
    "gptvnorm2048",
)


def upload_fields(conversation_id: str, image: ImageData) -> list[tuple[str, str]]:
    return [
        ("scenario", "UploadImage"),
        ("conversationId", conversation_id),
        ("FileBase64", f"data:{image.media_type};base64,{image.data}"),
        *(("optionsSets", option) for option in UPLOAD_OPTIONS),
    ]


async def upload_image(
    session: Session, settings: Settings, conversation_id: str, image: ImageData
) -> dict[str, Any]:
    if not session.mailbox:
        raise GatewayError(
            "Image upload needs mailbox data; sign in again with an image-capable Copilot session.",
            "image_upload_unavailable",
            503,
        )
    headers = {
        "Authorization": f"Bearer {session.token}",
        "Referer": "https://m365.cloud.microsoft/chat/",
        "x-anchormailbox": session.mailbox,
        "x-scenario": "officeweb",
        "x-variants": "feature.EnableImageSupportInUploadFile",
    }
    try:
        async with httpx.AsyncClient(verify=tls_context(), timeout=settings.timeout) as client:
            response = await client.post(
                "https://substrate.office.com/m365Copilot/UploadFile",
                files=[
                    (name, (None, value)) for name, value in upload_fields(conversation_id, image)
                ],
                headers=headers,
            )
        value = response.json()
        if (
            not response.is_success
            or not isinstance(value, dict)
            or value.get("result", {}).get("value") != "Success"
            or not isinstance(value.get("docId"), str)
        ):
            raise GatewayError("Copilot rejected image upload.", "image_upload_failed", 502)
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        raise GatewayError("Copilot image upload failed.", "image_upload_failed", 502) from None
    file_type = image.media_type.split("/")[1]
    return {
        "id": value["docId"],
        "messageAnnotationMetadata": {
            "@type": "File",
            "annotationType": "File",
            "fileType": file_type,
            "fileName": f"image.{file_type}",
        },
        "messageAnnotationType": "ImageFile",
    }


def build_request(
    session: Session,
    prompt: str,
    tone: str,
    conversation_id: str | None = None,
    annotations: Sequence[dict[str, Any]] = (),
) -> tuple[str, dict[str, Any]]:
    session_id, conversation = str(uuid.uuid4()), conversation_id or str(uuid.uuid4())
    parts = urlsplit(session.socket_url)
    query = parse_qs(parts.query)
    for name in ("chatsessionid", "XRoutingParameterSessionKey", "clientrequestid"):
        query[name] = [session_id.replace("-", "")]
    query["X-SessionId"] = [session_id]
    query["ConversationId"] = [conversation]
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), ""))
    argument = copy.deepcopy(session.template)
    argument.update(
        {
            "sessionId": session_id,
            "clientCorrelationId": session_id.replace("-", ""),
            "traceId": session_id.replace("-", ""),
            "isStartOfSession": True,
            "disconnectBehavior": "continue",
        }
    )
    argument["tone"] = tone
    if annotations:
        options = argument.get("optionsSets", [])
        if not isinstance(options, list):
            raise GatewayError("Invalid captured Copilot options.", "session_invalid", 503)
        argument["optionsSets"] = list(dict.fromkeys([*options, *VISION_CHAT_OPTIONS]))
    argument["message"] = {
        "author": "user",
        "text": prompt,
        "messageType": "Chat",
        "inputMethod": "Keyboard",
        "requestId": session_id.replace("-", ""),
        "locale": "en-US",
        "entityAnnotationTypes": ["People", "File", "Event", "Email", "TeamsMessage"],
    }
    if annotations:
        argument["message"]["messageAnnotations"] = list(annotations)
    return url, {"type": 4, "target": "chat", "invocationId": "0", "arguments": [argument]}


def response_update(frame: dict[str, Any]) -> tuple[str | None, bool]:
    frame_type = frame.get("type")
    if frame_type == 7:
        raise GatewayError("Upstream closed chat.", "upstream_protocol", 502)
    if frame_type == 3 and frame.get("error"):
        raise GatewayError("Upstream invocation failed.", "upstream_protocol", 502)
    containers = frame.get("arguments", []) if frame_type == 1 else [frame.get("item", {})]
    text = None
    final = frame_type in {2, 3}
    for container in containers:
        if not isinstance(container, dict):
            continue
        result = container.get("result", {})
        if isinstance(result, dict) and result.get("value") not in {None, "Success"}:
            raise GatewayError(
                "Upstream rejected request; refresh session or check access.",
                "upstream_rejected",
                502,
            )
        final = final or container.get("isFinal") is True
        for message in container.get("messages", []):
            if isinstance(message, dict) and message.get("author") == "bot":
                if (
                    message.get("messageType") in {"Progress", "ReferencesListComplete"}
                    or message.get("contentOrigin") in {"EarlyProgress", "ChainOfThoughtSummary"}
                    or message.get("addToChainOfThought") is True
                ):
                    continue
                if isinstance(message.get("text"), str):
                    text = message["text"]
                final = final or message.get("isFinal") is True
    return text, final


def tls_context() -> ssl.SSLContext:
    import truststore

    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class BrowserUpstream:
    def __init__(self, store: SecretStore, settings: Settings):
        self.store, self.settings = store, settings
        self.lock = asyncio.Semaphore(1)

    async def stream(
        self, prompt: str, tone: str, images: Sequence[ImageData] = ()
    ) -> AsyncIterator[str]:
        logger = logging.Logger("m365-private-websocket")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        deadline = asyncio.get_running_loop().time() + self.settings.timeout
        socket = None
        acquired = False
        try:
            async with asyncio.timeout_at(deadline):
                await self.lock.acquire()
                acquired = True
                session = await asyncio.to_thread(Session.load, self.store)
                conversation_id = str(uuid.uuid4())
                annotations = [
                    await upload_image(session, self.settings, conversation_id, image)
                    for image in images
                ]
                url, frame = build_request(session, prompt, tone, conversation_id, annotations)
                encoded = json.dumps(frame, separators=(",", ":")) + SEPARATOR
                if len(encoded.encode()) > MAX_FRAME_BYTES:
                    raise GatewayError(
                        "Prompt exceeds Copilot's ~2 MB request limit.", "context_limit", 413
                    )
                socket = await connect(
                    url,
                    origin="https://m365.cloud.microsoft",
                    ssl=tls_context(),
                    # Update frames repeat the whole answer; UTF-8 is up to 4 bytes/char.
                    max_size=4 * self.settings.max_output + 1_000_000,
                    open_timeout=20,
                    logger=logger,
                )
                await socket.send('{"protocol":"json","version":1}' + SEPARATOR)
                handshake = await socket.recv()
                if not isinstance(handshake, str) or {} not in decode_frames(handshake):
                    raise GatewayError("Upstream handshake failed.", "upstream_protocol", 502)
                await socket.send(encoded)
            previous = ""
            while True:
                async with asyncio.timeout_at(deadline):
                    payload = await socket.recv()
                if not isinstance(payload, str):
                    continue
                for update in decode_frames(payload):
                    text, final = response_update(update)
                    if text is not None and text != previous:
                        if not text.startswith(previous):
                            raise GatewayError(
                                "Upstream revised streamed text.", "upstream_revision", 502
                            )
                        if len(text) > self.settings.max_output:
                            raise GatewayError(
                                "Upstream output exceeds limit.", "output_limit", 502
                            )
                        delta = text[len(previous) :]
                        previous = text
                        yield delta
                    if final:
                        if not previous:
                            raise GatewayError(
                                "Upstream returned no text.", "upstream_protocol", 502
                            )
                        return
        except TimeoutError:
            raise GatewayError("Copilot request timed out.", "upstream_timeout", 504) from None
        except (WebSocketException, OSError):
            raise GatewayError(
                "Copilot connection failed; check network/session.", "upstream_connection", 502
            ) from None
        finally:
            with anyio.move_on_after(5, shield=True):
                if socket is not None:
                    await socket.close()
            if acquired:
                self.lock.release()
