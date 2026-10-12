import stat
from pathlib import Path
from typing import Any, ClassVar

import pytest
from telethon.sessions import StringSession

from app.telegram import login as login_module
from app.telegram.login import login, main, write_secret_file
from app.telegram.telethon_account import TEST_DC

SESSION = "1SECRET-SESSION-STRING-never-printed"


class FakeLoginClient:
    """Stands in for TelegramClient during login: records the prompts it was given."""

    instances: ClassVar[list["FakeLoginClient"]] = []

    def __init__(self, session: StringSession, api_id: int, api_hash: str) -> None:
        self.session = session
        self.args = (api_id, api_hash)
        self.started: dict[str, Any] = {}
        self.disconnected = False
        FakeLoginClient.instances.append(self)

    async def start(self, **kwargs: Any) -> "FakeLoginClient":
        self.started = {name: callback() for name, callback in kwargs.items()}
        self.session.save = lambda: SESSION  # type: ignore[method-assign]
        return self

    async def disconnect(self) -> None:
        self.disconnected = True


ENV = {"TG_API_ID": "12345", "TG_API_HASH": "hash-value"}


def prompts() -> dict[str, Any]:
    return {"phone": lambda: "+70000000000", "code": lambda: "11111", "password": lambda: "pw"}


async def test_login_writes_a_0600_file_and_prints_no_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "main.session.txt"
    await login(target, environ=ENV, client_factory=FakeLoginClient, **prompts())
    client = FakeLoginClient.instances[-1]
    assert client.args == (12345, "hash-value")
    assert client.started == {"phone": "+70000000000", "code_callback": "11111", "password": "pw"}
    assert client.disconnected
    assert target.read_text() == SESSION
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    out, err = capsys.readouterr()
    assert SESSION not in out + err and "hash-value" not in out + err
    assert str(target) in out


async def test_existing_file_is_tightened_to_0600(tmp_path: Path) -> None:
    target = tmp_path / "s.txt"
    target.write_text("old value that is longer than the new one" * 3)
    target.chmod(0o644)
    await login(target, environ=ENV, client_factory=FakeLoginClient, **prompts())
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text() == SESSION


async def test_test_dc(tmp_path: Path) -> None:
    await login(
        tmp_path / "t.txt", test_dc=True, environ=ENV, client_factory=FakeLoginClient, **prompts()
    )
    session = FakeLoginClient.instances[-1].session
    assert (session.dc_id, session.server_address, session.port) == TEST_DC


async def test_missing_api_credentials(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="TG_API_ID and TG_API_HASH"):
        await login(tmp_path / "x", environ={}, client_factory=FakeLoginClient, **prompts())
    assert not (tmp_path / "x").exists()


def test_symlink_is_not_followed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.write_text("")
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(OSError):
        write_secret_file(link, SESSION)
    assert real.read_text() == ""


def test_cli_parses_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[Path, bool]] = []

    async def fake_login(path: Path, *, test_dc: bool) -> None:
        calls.append((path, test_dc))

    monkeypatch.setattr(login_module, "login", fake_login)
    main(["--session-file", str(tmp_path / "a"), "--test-dc"])
    main(["--session-file", str(tmp_path / "b")])
    assert calls == [(tmp_path / "a", True), (tmp_path / "b", False)]
    with pytest.raises(SystemExit):
        main([])
    assert "--session-file" in capsys.readouterr().err
