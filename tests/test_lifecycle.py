import json
from types import SimpleNamespace

import pytest
from test_sessions import socket_url

from m365_agent_gateway import browser, cli, upstream
from m365_agent_gateway.config import Settings
from m365_agent_gateway.errors import GatewayError
from m365_agent_gateway.sessions import SecretStore, Session


class MemoryStore:
    def __init__(self):
        self.values = {}

    def get(self, name):
        return self.values.get(name)

    def put(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)


def test_session_keyring_roundtrip_and_logout():
    store = MemoryStore()
    with pytest.raises(GatewayError):
        Session.load(store)
    session = Session.from_capture(socket_url(), {"source": "officeweb"}, "copilot@example.test")
    session.save(store)
    assert Session.load(store).tenant == session.tenant
    assert Session.load(store).mailbox == "copilot@example.test"
    store.put("session", "bad")
    with pytest.raises(GatewayError):
        Session.load(store)
    store.delete("session")


def test_secure_store_delegates_without_plaintext(monkeypatch):
    from keyring.backends.macOS import Keyring

    memory = MemoryStore()
    monkeypatch.setattr(Keyring, "get_password", lambda self, service, name: memory.get(name))
    monkeypatch.setattr(
        Keyring, "set_password", lambda self, service, name, value: memory.put(name, value)
    )
    monkeypatch.setattr(Keyring, "delete_password", lambda self, service, name: memory.delete(name))
    monkeypatch.setattr("m365_agent_gateway.sessions.sys.platform", "darwin")
    store = SecretStore()
    store.put("test", "private")
    assert store.get("test") == "private"
    store.delete("test")
    assert store.get("test") is None


class FakeSocket:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.sent = []
        self.closed = False

    async def send(self, payload):
        self.sent.append(payload)

    async def recv(self):
        value = next(self.messages)
        if isinstance(value, Exception):
            raise value
        return value

    async def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "frames, expected, error",
    [
        (
            [
                "{}\x1e",
                '{"type":1,"arguments":[{"messages":[{"author":"bot","text":"hi"}]}]}\x1e',
                '{"type":2,"item":{"messages":[{"author":"bot","text":"hi there"}]}}\x1e',
            ],
            "hi there",
            None,
        ),
        (['{"error":"secret"}\x1e'], "", "upstream_protocol"),
        (["{}\x1e", '{"type":2,"item":{}}\x1e'], "", "upstream_protocol"),
        (["{}\x1e", OSError("secret bearer")], "", "upstream_connection"),
        (
            [
                "{}\x1e",
                '{"type":1,"arguments":[{"messages":[{"author":"bot","text":"first"}]}]}\x1e',
                '{"type":2,"item":{"messages":[{"author":"bot","text":"revised"}]}}\x1e',
            ],
            "",
            "upstream_revision",
        ),
    ],
)
async def test_socket_lifecycle(monkeypatch, frames, expected, error):
    store = MemoryStore()
    Session.from_capture(socket_url(), {"source": "officeweb"}).save(store)
    socket = FakeSocket(frames)

    async def connect(*args, **kwargs):
        assert kwargs["ssl"] is not None
        return socket

    monkeypatch.setattr(upstream, "connect", connect)
    transport = upstream.BrowserUpstream(store, Settings())
    if error:
        with pytest.raises(GatewayError) as caught:
            _ = [part async for part in transport.stream("hi", "Magic")]
        assert caught.value.code == error
        assert "secret" not in str(caught.value)
    else:
        assert "".join([part async for part in transport.stream("hi", "Magic")]) == expected
    assert socket.closed
    assert not transport.lock.locked()


async def test_socket_cancel_releases_lock(monkeypatch):
    store = MemoryStore()
    Session.from_capture(socket_url(), {}).save(store)
    socket = FakeSocket(
        ["{}\x1e", '{"type":1,"arguments":[{"messages":[{"author":"bot","text":"hi"}]}]}\x1e']
    )

    async def connect(*args, **kwargs):
        return socket

    monkeypatch.setattr(upstream, "connect", connect)
    transport = upstream.BrowserUpstream(store, Settings())
    stream = transport.stream("hi", "Magic")
    assert await anext(stream) == "hi"
    await stream.aclose()
    assert socket.closed and not transport.lock.locked()


async def test_browser_capture_is_keyring_only(monkeypatch):
    store = MemoryStore()
    handlers = {}
    closed = []

    class Socket:
        url = socket_url()

        def on(self, event, callback):
            callback(
                json.dumps(
                    {
                        "target": "chat",
                        "arguments": [
                            {"source": "officeweb", "message": {"text": "private warmup"}}
                        ],
                    }
                )
                + "\x1e"
            )

    class Page:
        def on(self, event, callback):
            handlers[event] = callback

        async def goto(self, *args, **kwargs):
            handlers["request"](
                SimpleNamespace(
                    url="https://substrate.office.com/search/api/v1/suggestions",
                    headers={"x-anchormailbox": "copilot@example.test"},
                )
            )
            handlers["websocket"](Socket())

    class Context:
        def on(self, event, callback):
            handlers[event] = callback

        async def new_page(self):
            page = Page()
            handlers["page"](page)
            return page

        async def storage_state(self):
            return {"cookies": [], "origins": []}

    class Browser:
        async def new_context(self, **kwargs):
            return Context()

        async def close(self):
            closed.append(True)

    class Chromium:
        async def launch(self, **kwargs):
            return Browser()

    class Playwright:
        async def __aenter__(self):
            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(browser, "async_playwright", Playwright)
    await browser.login(store, timeout=0.1)
    assert "private warmup" not in store.get("session")
    assert Session.load(store).mailbox == "copilot@example.test"
    assert "browser-state" in store.values
    assert closed


def test_cli_status_and_logout(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    store = MemoryStore()
    monkeypatch.setattr(cli, "SecretStore", lambda: store)
    monkeypatch.setattr("sys.argv", ["gateway", "status"])
    cli.main()
    assert "login_required" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["gateway", "logout"])
    cli.main()
    assert "removed" in capsys.readouterr().out


def test_cli_serve_enables_access_logs(monkeypatch):
    import uvicorn

    store = MemoryStore()
    options = {}
    monkeypatch.setattr(cli, "SecretStore", lambda: store)
    monkeypatch.setattr(cli, "BrowserUpstream", lambda store, settings: object())
    monkeypatch.setattr(cli, "create_app", lambda settings, upstream: object())
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: options.update(kwargs))
    monkeypatch.setattr("sys.argv", ["gateway", "serve"])

    cli.main()

    assert options["access_log"] is True
    assert options["log_level"] == "info"
