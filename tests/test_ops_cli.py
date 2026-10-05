import stat
import subprocess
import sys

import pytest
from test_docker_cli import MemoryStore as BaseStore
from test_docker_cli import capture

from m365_agent_gateway import cli
from m365_agent_gateway.clients import write_configs
from m365_agent_gateway.errors import GatewayError


class MemoryStore(BaseStore):
    def delete(self, name):
        self.values.pop(name, None)


def run(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["gateway", *argv])
    cli.main()


def test_bad_port_env_is_a_clean_error(monkeypatch, capsys):
    monkeypatch.setenv("M365_PORT", "abc")
    for command in (["status"], ["serve"], ["configure"], ["client", "claude"]):
        with pytest.raises(SystemExit) as raised:
            run(monkeypatch, *command)
        assert raised.value.code == 1
        assert "M365_PORT" in capsys.readouterr().err


def test_serve_refuses_non_loopback_outside_container(monkeypatch, capsys):
    import uvicorn

    started = []
    monkeypatch.delenv("M365_SESSION_FILE", raising=False)
    monkeypatch.setattr(cli, "SecretStore", MemoryStore)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append(k))
    with pytest.raises(SystemExit) as raised:
        run(monkeypatch, "serve", "--host", "0.0.0.0")
    assert raised.value.code == 1
    assert "container" in capsys.readouterr().err
    assert not started


def test_serve_allows_non_loopback_in_container(monkeypatch, tmp_path):
    import uvicorn

    started = []
    monkeypatch.setenv("M365_SESSION_FILE", str(tmp_path / "session.json"))
    monkeypatch.setattr(cli, "BrowserUpstream", lambda store, settings: object())
    monkeypatch.setattr(cli, "create_app", lambda settings, upstream: object())
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: started.append(kwargs))
    run(monkeypatch, "serve", "--host", "0.0.0.0")
    assert started[0]["host"] == "0.0.0.0"


def test_logout_removes_exported_session(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("M365_SESSION_FILE", raising=False)
    monkeypatch.setattr(cli, "SecretStore", MemoryStore)
    exported = tmp_path / "m365-agent-gateway/docker/session.json"
    exported.parent.mkdir(parents=True)
    exported.write_text("{}")
    run(monkeypatch, "logout")
    assert not exported.exists()
    assert "docker compose down" in capsys.readouterr().out
    run(monkeypatch, "logout")


def test_docker_up_watch_retries_failed_refresh_without_browser(monkeypatch, tmp_path):
    store = MemoryStore()
    capture(store, 3600)
    commands = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, env: commands.append(env) or subprocess.CompletedProcess(args, 0),
    )
    monkeypatch.setattr(cli, "export_session", lambda *a: None)
    monkeypatch.setattr(
        cli, "login", lambda *a, **k: (_ for _ in ()).throw(AssertionError("interactive login"))
    )
    outcomes = [GatewayError("expired", "login_required", 503)] * 2 + [None]
    interactive_flags = []

    def ensure(target, interactive=True):
        interactive_flags.append(interactive)
        outcome = outcomes.pop(0) if len(interactive_flags) > 1 and outcomes else None
        if outcome:
            raise outcome

    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 5:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "ensure_session", ensure)
    monkeypatch.setattr(cli.time, "sleep", sleep)
    cli.docker_up(store, tmp_path / "docker", watch=True)
    assert set(interactive_flags[1:]) == {False}
    assert sleeps[1] == 60 and sleeps[2] == 120
    assert sleeps[3] > 120
    env = commands[0]
    assert env["M365_SESSION_DIR"] == str((tmp_path / "docker").resolve())
    assert env["M365_UID"].isdigit() and env["M365_GID"].isdigit()


def test_ensure_session_without_interactive_fallback_raises(monkeypatch):
    store = MemoryStore()
    capture(store, 300)

    async def fake_login(target, headless=False):
        raise GatewayError("expired", "login_required", 503)

    monkeypatch.setattr(cli, "login", fake_login)
    with pytest.raises(GatewayError):
        cli.ensure_session(store, interactive=False)


def test_configure_does_not_chmod_existing_directory(tmp_path):
    existing = tmp_path / "home"
    existing.mkdir(mode=0o755)
    existing.chmod(0o755)
    write_configs(existing, 47821)
    assert stat.S_IMODE(existing.stat().st_mode) == 0o755


def test_configure_unwritable_directory_is_a_gateway_error(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(GatewayError, match="Cannot write client configs"):
            write_configs(locked / "clients", 47821)
        with pytest.raises(GatewayError, match="Cannot write client configs"):
            write_configs(locked, 47821)
    finally:
        locked.chmod(0o700)


def test_serve_does_not_import_playwright():
    code = (
        "import sys; from m365_agent_gateway import cli; cli.create_app; "
        "from m365_agent_gateway.upstream import BrowserUpstream; "
        "assert 'playwright' not in sys.modules, 'playwright imported'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
