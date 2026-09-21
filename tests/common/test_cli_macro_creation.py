import asyncio
import io
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from keymasq.cli import __main__ as cli_main
from keymasq.cli import commands
from keymasq.common.ipc import Command, CommandType, Response
from keymasq.common.types import JsonObject
from keymasq.keymasqd.daemon_macro_commands import handle_macro_command
from keymasq.keymasqd.macro_store import MacroStore
from keymasq.session.manager.command import macro as session_macro


@pytest.mark.parametrize("name", ["say_hello", "say hello"])
@pytest.mark.parametrize("json_output", [False, True])
def test_cli_force_creates_then_updates_stored_macro(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    json_output: bool,
) -> None:
    store = MacroStore(tmp_path / "macros")
    daemon = Mock(macro_store=store)
    manager = Mock()

    async def send_command(command: Command) -> Response:
        try:
            result = await handle_macro_command(daemon, command.command, command.data)
        except (OSError, ValueError) as exc:
            return Response(status="error", error=str(exc))
        return Response(status="ok", data=result)

    manager.client.send_command = send_command
    monkeypatch.setattr(session_macro.coordinator, "refresh_macro_bindings", AsyncMock())

    def session_request(payload: JsonObject) -> JsonObject | None:
        return asyncio.run(
            session_macro.handle_macro_commands(manager, str(payload["command"]), payload)
        )

    monkeypatch.setattr(commands, "_session_request", session_request)

    for revision, text in enumerate(["hello<enter>", "goodbye<enter>"], start=1):
        monkeypatch.setattr(sys, "argv", ["keymasq", "type", text, "--print-json"])
        cli_main.main()
        compiled = capsys.readouterr().out
        monkeypatch.setattr(sys, "stdin", io.StringIO(compiled))
        argv = ["keymasq", "macros", "create", name, "--force"]
        if json_output:
            argv.append("--json")
        monkeypatch.setattr(sys, "argv", argv)
        cli_main.main()

        output = capsys.readouterr().out
        if json_output:
            assert json.loads(output)["status"] == "ok"
        else:
            assert output.strip() == f"Saved macro: {name}"
        stored = store.get(name)
        assert stored["revision"] == revision
        assert stored["events"] == json.loads(compiled)["events"]
        assert len(store.list_meta()) == 1

    before = store.get(name)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"events":[]}'))
    monkeypatch.setattr(sys, "argv", ["keymasq", "macros", "create", name])
    with pytest.raises(SystemExit) as excinfo:
        cli_main.main()
    assert excinfo.value.code == 1
    assert "already exists" in capsys.readouterr().out
    assert store.get(name) == before


def test_force_does_not_update_after_unrelated_create_failure() -> None:
    store = Mock()
    store.create.side_effect = PermissionError("read-only store")
    daemon = Mock(macro_store=store)
    with pytest.raises(PermissionError, match="read-only store"):
        asyncio.run(
            handle_macro_command(
                daemon,
                CommandType.MACRO_CREATE,
                {"macro": {"name": "test", "events": []}, "overwrite": True},
            )
        )
    store.update.assert_not_called()
