import json
import stat

import pytest
from test_api import FakeUpstream

from m365_agent_gateway.clients import write_configs
from m365_agent_gateway.config import Settings
from m365_agent_gateway.errors import GatewayError
from m365_agent_gateway.mcp_server import create_mcp


def test_private_client_configs(tmp_path):
    paths = write_configs(tmp_path / "clients", 47821)
    assert {path.name for path in paths} == {
        "claude.settings.json",
        "opencode.json",
        "codex.toml",
        "mcp.json",
    }
    for path in paths:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    claude = json.loads(paths[0].read_text())
    assert claude["env"]["ANTHROPIC_MODEL"] == "m365-agent-sonnet"
    assert claude["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:47821"
    assert claude["env"]["ANTHROPIC_AUTH_TOKEN"] == "local"
    assert "apiKey" not in json.loads(paths[1].read_text())["provider"]["m365"]["options"]
    assert "M365_GATEWAY_API_KEY" not in paths[2].read_text()
    with pytest.raises(GatewayError):
        write_configs(tmp_path / "clients", 47821)


async def test_mcp_tool_discovery_and_chat():
    server = create_mcp(Settings(), FakeUpstream())
    names = {tool.name for tool in await server.list_tools()}
    assert names == {"copilot_chat", "copilot_decide_tools", "gateway_capabilities"}
    response = await server.call_tool("copilot_chat", {"prompt": "hello"})
    assert "Hello world" in str(response)
