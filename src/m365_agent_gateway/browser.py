"""Explicit user-assisted capture in an ephemeral browser profile."""

import asyncio
import json
import sys
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Request, WebSocket, async_playwright

from .errors import GatewayError
from .sessions import SecretStore, Session, trusted_socket
from .upstream import decode_frames

PROBE_MESSAGE = "hi"


def drop_cached_chat_token(state: dict[str, Any]) -> dict[str, Any]:
    # MSAL reuses a cached token until near expiry; removing it forces a silent renewal.
    for origin in state.get("origins", []):
        origin["localStorage"] = [
            item
            for item in origin.get("localStorage", [])
            if not ("|accesstoken|" in item.get("name", "") and "/sydney/" in item["name"])
        ]
    return state


async def send_probe(page: Page) -> None:
    if urlsplit(page.url).hostname != "m365.cloud.microsoft":
        raise GatewayError("Saved sign-in expired; run login.", "login_required", 503)
    box = page.locator('[contenteditable="true"], textarea').first
    await box.wait_for(timeout=45_000)
    # The composer renders before its send handler is wired up.
    await page.wait_for_timeout(12_000)
    await box.click()
    await box.type(PROBE_MESSAGE)
    await page.keyboard.press("Enter")


async def login(store: SecretStore, timeout: float = 600, headless: bool = False) -> None:
    captured: dict[str, Any] = {}
    ready = asyncio.Event()
    state = store.get("browser-state")
    if headless and not state:
        raise GatewayError("No saved browser sign-in; run login first.", "login_required", 503)

    def inspect_request(request: Request) -> None:
        try:
            if urlsplit(request.url).hostname == "substrate.office.com":
                mailbox = request.headers.get("x-anchormailbox")
                if isinstance(mailbox, str) and mailbox:
                    captured["mailbox"] = mailbox
        except (ValueError, PlaywrightError):
            return

    def inspect_socket(socket: WebSocket) -> None:
        if not trusted_socket(socket.url):
            return

        def inspect_sent(payload: str | bytes) -> None:
            if not isinstance(payload, str) or len(payload) > 1_000_000:
                return
            try:
                for frame in decode_frames(payload):
                    arguments = frame.get("arguments")
                    if frame.get("target") == "chat" and isinstance(arguments, list) and arguments:
                        if isinstance(arguments[0], dict):
                            session = Session.from_capture(
                                socket.url, arguments[0], captured.get("mailbox")
                            )
                            captured["session"] = session
                            ready.set()
            except GatewayError:
                return

        socket.on("framesent", inspect_sent)

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=headless)
            try:
                saved = json.loads(state) if state else None
                context = await browser.new_context(
                    storage_state=drop_cached_chat_token(saved) if headless else saved
                )
                context.on("request", inspect_request)
                context.on("page", lambda page: page.on("websocket", inspect_socket))
                page = await context.new_page()
                if not headless:
                    print(
                        "Sign in in opened browser. Send one harmless Copilot message.",
                        file=sys.stderr,
                    )
                await page.goto("https://m365.cloud.microsoft/chat/", wait_until="domcontentloaded")
                if headless:
                    await send_probe(page)
                await asyncio.wait_for(ready.wait(), 60 if headless else timeout)
                captured["session"].save(store)
                store.put("browser-state", json.dumps(await context.storage_state()))
                print("Session saved in OS credential store.", file=sys.stderr)
            finally:
                await browser.close()
    except TimeoutError:
        if headless:
            raise GatewayError(
                "Headless refresh failed; run login interactively.", "login_required", 503
            ) from None
        raise GatewayError(
            "Login timed out; send a Copilot message in the browser.", "login_timeout", 503
        ) from None
    except (PlaywrightError, ValueError):
        raise GatewayError(
            "Browser login failed; install Chromium and retry.", "login_failed", 503
        ) from None
