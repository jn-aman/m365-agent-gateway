"""Local lifecycle and explicitly user-initiated client commands."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .api import create_app
from .browser import login
from .clients import launch_client, write_configs
from .config import Settings, state_dir
from .errors import GatewayError
from .mcp_server import create_mcp
from .sessions import FileStore, SecretStore, Session, export_session
from .upstream import BrowserUpstream


def main() -> None:
    parser = argparse.ArgumentParser(description="Local macOS/Linux M365 agent gateway")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="Sign in interactively and capture a chat session")
    refresh = sub.add_parser("refresh", help="Refresh session through a browser")
    refresh.add_argument("--headless", action="store_true", help="Reuse saved sign-in, no window")
    sub.add_parser("logout", help="Delete locally saved session and browser state")
    sub.add_parser("status", help="Show safe session expiry and backend status")
    configure = sub.add_parser("configure", help="Write private client configuration files")
    configure.add_argument("--output", type=Path, default=state_dir() / "clients")
    configure.add_argument("--port", type=int, default=Settings.from_env().port)
    serve = sub.add_parser("serve", help="Run localhost HTTP service")
    serve.add_argument("--port", type=int, default=Settings.from_env().port)
    # Containers bind 0.0.0.0 and rely on a loopback-only port publish.
    serve.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "0.0.0.0"])
    export = sub.add_parser("export-session", help="Write session file for the Docker image")
    export.add_argument("--output", type=Path, default=state_dir() / "docker")
    docker = sub.add_parser("docker-up", help="Sign in, start Docker gateway, keep session fresh")
    docker.add_argument("--output", type=Path, default=state_dir() / "docker")
    docker.add_argument("--no-watch", action="store_true", help="Start and exit, no refresh loop")
    sub.add_parser("mcp", help="Run stdio MCP tools")
    client = sub.add_parser("client", help="Launch installed coding client through local gateway")
    client.add_argument("name", choices=["claude", "opencode", "codex"])
    client.add_argument("--port", type=int, default=Settings.from_env().port)
    client.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        session_file = os.environ.get("M365_SESSION_FILE")
        store = FileStore(Path(session_file)) if session_file else SecretStore()
        settings = Settings.from_env()
        if args.command == "login":
            asyncio.run(login(store))
        elif args.command == "refresh":
            asyncio.run(login(store, headless=args.headless))
        elif args.command == "docker-up":
            docker_up(store, args.output, watch=not args.no_watch)
        elif args.command == "export-session":
            print(export_session(store, args.output))
        elif args.command == "logout":
            store.delete("session")
            store.delete("browser-state")
            store.delete("api-key")
            print("Local session removed. Microsoft browser/account sessions are not revoked.")
        elif args.command == "status":
            try:
                session = Session.load(store)
                report = {
                    "session": "available",
                    "expires_at": datetime.fromtimestamp(session.expires_at, UTC).isoformat(),
                    "live_verified": False,
                }
            except GatewayError as error:
                report = {"session": error.code, "live_verified": False}
            print(json.dumps(report))
        elif args.command == "configure":
            for path in write_configs(args.output, args.port):
                print(path)
        elif args.command == "serve":
            import uvicorn

            if not 1 <= args.port <= 65535:
                raise GatewayError("Invalid local port.")
            app = create_app(settings, BrowserUpstream(store, settings))
            uvicorn.run(app, host=args.host, port=args.port, access_log=True, log_level="info")
        elif args.command == "mcp":
            create_mcp(settings, BrowserUpstream(store, settings)).run(transport="stdio")
        elif args.command == "client":
            directory = state_dir() / f"clients-{args.port}"
            if not directory.exists():
                write_configs(directory, args.port)
            extra = args.args[1:] if args.args[:1] == ["--"] else args.args
            raise SystemExit(launch_client(args.name, directory, args.port, extra))
    except GatewayError as error:
        print(f"{error.code}: {error}", file=sys.stderr)
        raise SystemExit(1) from None


REFRESH_MARGIN = 600


def ensure_session(store: SecretStore) -> Session:
    try:
        session = Session.load(store)
        if session.expires_at - time.time() > REFRESH_MARGIN:
            return session
    except GatewayError:
        pass
    try:
        asyncio.run(login(store, headless=True))
    except GatewayError as error:
        print(f"{error.code}: {error} Opening browser.", file=sys.stderr)
        asyncio.run(login(store))
    return Session.load(store)


def docker_up(store: SecretStore, directory: Path, watch: bool) -> None:
    compose = Path(__file__).resolve().parents[2] / "compose.yaml"
    if not compose.is_file():
        raise GatewayError("compose.yaml not found; run from a source checkout.")
    ensure_session(store)
    export_session(store, directory)
    environment = {**os.environ, "M365_SESSION_DIR": str(directory)}
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose), "up", "-d", "--build"], env=environment
    )
    if result.returncode:
        raise GatewayError("docker compose up failed.", "docker", 503)
    print("Gateway container running on http://127.0.0.1:47821", file=sys.stderr)
    try:
        while watch:
            try:
                wait = Session.load(store).expires_at - time.time() - REFRESH_MARGIN
            except GatewayError:
                wait = 0
            # Floor also covers laptop sleep, where the token may already be expired.
            time.sleep(max(30, wait))
            ensure_session(store)
            export_session(store, directory)
            print("Session refreshed and exported.", file=sys.stderr)
    except KeyboardInterrupt:
        print("Stopped refreshing; container keeps running.", file=sys.stderr)
