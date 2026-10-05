"""Stdio MCP tools; tool execution remains owned by the MCP client."""

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .config import Settings
from .normalize import normalize
from .service import Result, Upstream, generate


def create_mcp(settings: Settings, upstream: Upstream) -> FastMCP:
    server = FastMCP("m365-agent-gateway")
    annotations = ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    )

    @server.tool(annotations=annotations)
    async def copilot_chat(prompt: str) -> dict[str, Any]:
        """Ask signed-in Copilot. Creates a cloud conversation; sends prompt to Microsoft."""
        turn = normalize({"messages": [{"role": "user", "content": prompt}]}, "chat", settings)
        async for item in generate(turn, upstream, settings):
            if isinstance(item, Result):
                return {"text": item.reply.text, "limited": item.limited}
        return {"text": ""}

    @server.tool(annotations=annotations)
    async def copilot_decide_tools(
        messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Return validated experimental tool decisions. Never executes tools or shell commands."""
        turn = normalize({"messages": messages, "tools": tools}, "chat", settings)
        async for item in generate(turn, upstream, settings):
            if isinstance(item, Result):
                return {
                    "text": item.reply.text,
                    "calls": [
                        {"id": call.id, "name": call.name, "arguments": call.arguments}
                        for call in item.reply.calls
                    ],
                    "experimental": True,
                }
        return {"text": "", "calls": []}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def gateway_capabilities() -> dict[str, Any]:
        """Report local protocol capabilities without accessing account data."""
        return {
            "model": settings.model,
            "streaming": True,
            "tool_emulation": settings.emulate_tools,
            "native_tool_calling": False,
            "shell_execution": False,
            "usage": "estimated",
            "backend": "browser-session",
        }

    return server
