import asyncio
import socket
import sys

import pytest
import uvicorn
from anthropic import AsyncAnthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI
from test_api import MODEL, FakeUpstream

from m365_agent_gateway.api import create_app
from m365_agent_gateway.config import Settings

CLIENT_KEY = "unused-local-client-key"


@pytest.fixture
async def sdk_server():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    app = create_app(Settings(), FakeUpstream())
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(3):
            while not server.started:
                if task.done():
                    task.result()
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()


async def test_openai_sdk_streams(sdk_server):
    async with AsyncOpenAI(
        api_key=CLIENT_KEY, base_url=sdk_server + "/v1", max_retries=0
    ) as client:
        stream = await client.chat.completions.create(
            model=MODEL, stream=True, messages=[{"role": "user", "content": "hi"}]
        )
        parts = [chunk.choices[0].delta.content or "" async for chunk in stream]
        assert "".join(parts) == "Hello world"
        async with client.responses.stream(model=MODEL, input="hi") as response:
            events = [event.type async for event in response]
            final = await response.get_final_response()
            assert "response.output_text.delta" in events
            assert final.output_text == "Hello world"


async def test_anthropic_sdk_stream(sdk_server):
    async with AsyncAnthropic(api_key=CLIENT_KEY, base_url=sdk_server, max_retries=0) as client:
        async with client.messages.stream(
            model=MODEL, max_tokens=100, messages=[{"role": "user", "content": "hi"}]
        ) as stream:
            parts = [part async for part in stream.text_stream]
            final = await stream.get_final_message()
            assert "".join(parts) == "Hello world"
            assert final.stop_reason == "end_turn"


async def test_actual_mcp_stdio_handshake():
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "m365_agent_gateway", "mcp"]
    )
    async with (
        stdio_client(parameters) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        listing = await session.list_tools()
        assert len(listing.tools) == 3
        result = await session.call_tool("gateway_capabilities", {})
        assert not result.isError
        assert '"native_tool_calling":false' in result.content[0].text.replace(" ", "")
