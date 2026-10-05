import base64
import json
import time
from urllib.parse import quote

import pytest

from m365_agent_gateway.errors import GatewayError
from m365_agent_gateway.sessions import FileStore, Session, export_session, sanitize_template


def jwt(**overrides):
    claims = {
        "oid": "user-id",
        "tid": "tenant-id",
        "exp": int(time.time()) + 3600,
        "aud": "https://substrate.office.com/sydney",
        "scp": "sydney.readwrite",
    }
    claims.update(overrides)
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "header." + encoded + ".signature"


def socket_url(token=None):
    return "wss://substrate.office.com/m365Copilot/Chathub/user-id@tenant-id?access_token=" + quote(
        token or jwt()
    )


def test_session_accepts_expected_service():
    session = Session.from_capture(socket_url(), {"source": "officeweb", "message": {}})
    assert session.tenant == "tenant-id"
    assert session.expires_at > time.time()
    assert session.token not in repr(session)


def test_exported_session_file_round_trips_read_only(tmp_path):
    class MemoryStore:
        def __init__(self):
            self.values = {}

        def get(self, name):
            return self.values.get(name)

        def put(self, name, value):
            self.values[name] = value

    source = MemoryStore()
    Session.from_capture(socket_url(), {"source": "officeweb"}, "user@example.test").save(source)
    path = export_session(source, tmp_path / "docker")
    assert path.stat().st_mode & 0o777 == 0o600
    store = FileStore(path)
    assert Session.load(store).mailbox == "user@example.test"
    assert store.get("browser-state") is None
    with pytest.raises(GatewayError):
        store.put("session", "{}")
    assert FileStore(tmp_path / "missing.json").get("session") is None


@pytest.mark.parametrize(
    "url",
    [
        "wss://substrate.office.com.evil.test/m365Copilot/Chathub/",
        "wss://evil.test/m365Copilot/Chathub/",
        "ws://substrate.office.com/m365Copilot/Chathub/",
        "wss://user@substrate.office.com/m365Copilot/Chathub/",
        "wss://substrate.office.com:444/m365Copilot/Chathub/",
    ],
)
def test_session_rejects_untrusted_destination(url):
    with pytest.raises(GatewayError):
        Session.from_capture(url, {})


def test_expired_or_wrong_audience_tokens_rejected():
    for token in [jwt(exp=0), jwt(aud="https://graph.microsoft.com", scp="User.Read"), "bad"]:
        with pytest.raises(GatewayError):
            Session.from_capture(socket_url(token), {})


def test_template_drops_initial_prompt_and_attachments():
    template = sanitize_template(
        {
            "source": "officeweb",
            "optionsSets": ["someflag"],
            "message": {"text": "private", "messageAnnotations": [{"id": "private"}]},
            "conversationId": "old",
            "clientInfo": {"clientPlatform": "web"},
        }
    )
    assert "private" not in json.dumps(template)
    assert template["optionsSets"] == ["someflag"]
