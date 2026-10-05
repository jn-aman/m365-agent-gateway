import time

from test_sessions import jwt, socket_url

from m365_agent_gateway import cli
from m365_agent_gateway.sessions import Session


class MemoryStore:
    def __init__(self):
        self.values = {}

    def get(self, name):
        return self.values.get(name)

    def put(self, name, value):
        self.values[name] = value


def capture(store, seconds):
    Session.from_capture(socket_url(jwt(exp=int(time.time()) + seconds)), {}).save(store)


def test_fresh_session_skips_browser(monkeypatch):
    store = MemoryStore()
    capture(store, 3600)
    monkeypatch.setattr(cli, "login", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert cli.ensure_session(store).expires_at > time.time() + 3000


def test_expiring_session_refreshes_headless_then_falls_back(monkeypatch):
    store = MemoryStore()
    capture(store, 300)
    calls = []

    async def fake_login(target, headless=False):
        calls.append(headless)
        if headless:
            raise cli.GatewayError("expired", "login_required", 503)
        capture(target, 3600)

    monkeypatch.setattr(cli, "login", fake_login)
    assert cli.ensure_session(store).expires_at > time.time() + 3000
    assert calls == [True, False]


def test_headless_refresh_drops_only_cached_chat_token():
    from m365_agent_gateway.browser import drop_cached_chat_token

    names = [
        "msal.3|u.t|login.windows.net|accesstoken|c|t|https://substrate.office.com/sydney/.default|",
        "msal.3|u.t|login.windows.net|accesstoken|c|t|https://graph.microsoft.com/.default|",
        "msal.3|u.t|login.windows.net|refreshtoken|c|||",
    ]
    state = {"origins": [{"localStorage": [{"name": n, "value": "v"} for n in names]}]}
    kept = [i["name"] for i in drop_cached_chat_token(state)["origins"][0]["localStorage"]]
    assert kept == names[1:]
