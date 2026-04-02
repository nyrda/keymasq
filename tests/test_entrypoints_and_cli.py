import runpy
import sys
import types
from collections.abc import Callable

import pytest


def _module_with_main(fn: Callable[[], None]) -> types.ModuleType:
    module = types.ModuleType("stub_module")
    module.main = fn
    return module


def test_root_entrypoint_prefers_cli_when_args(monkeypatch: pytest.MonkeyPatch) -> None:
    from keyforge import __main__ as app_main

    called: list[str] = []
    monkeypatch.setitem(
        sys.modules,
        "keyforge.cli.__main__",
        _module_with_main(lambda: called.append("cli")),
    )
    monkeypatch.setitem(
        sys.modules,
        "keyforge.gui.__main__",
        _module_with_main(lambda: called.append("gui")),
    )
    monkeypatch.setattr(sys, "argv", ["keyforge", "devices"])

    app_main.main()
    assert called == ["cli"]


def test_root_entrypoint_uses_gui_with_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    from keyforge import __main__ as app_main

    called: list[str] = []
    monkeypatch.setitem(
        sys.modules,
        "keyforge.cli.__main__",
        _module_with_main(lambda: called.append("cli")),
    )
    monkeypatch.setitem(
        sys.modules,
        "keyforge.gui.__main__",
        _module_with_main(lambda: called.append("gui")),
    )
    monkeypatch.setattr(sys, "argv", ["keyforge"])
    monkeypatch.setenv("DISPLAY", ":0")

    app_main.main()
    assert called == ["gui"]


def test_root_entrypoint_falls_back_to_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    from keyforge import __main__ as app_main

    called: list[str] = []
    monkeypatch.setitem(
        sys.modules,
        "keyforge.cli.__main__",
        _module_with_main(lambda: called.append("cli")),
    )
    monkeypatch.setitem(
        sys.modules,
        "keyforge.gui.__main__",
        _module_with_main(lambda: called.append("gui")),
    )
    monkeypatch.setattr(sys, "argv", ["keyforge"])
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    monkeypatch.delenv("DESKTOP_SESSION", raising=False)

    app_main.main()
    assert called == ["cli"]


def test_keyforged_script_entrypoint_calls_daemon_main(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setitem(
        sys.modules,
        "keyforge.keyforged.daemon",
        _module_with_main(lambda: called.append("daemon")),
    )

    runpy.run_module("keyforge.keyforged.__main__", run_name="__main__")
    assert called == ["daemon"]


def test_session_script_entrypoint_calls_manager_main(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setitem(
        sys.modules,
        "keyforge.session.manager",
        _module_with_main(lambda: called.append("manager")),
    )

    runpy.run_module("keyforge.session.__main__", run_name="__main__")
    assert called == ["manager"]


def test_cli_main_hardware_create_requires_vid_pid(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from keyforge.cli import __main__ as cli_main

    monkeypatch.setattr(sys, "argv", ["keyforge", "hardware", "create"])

    with pytest.raises(SystemExit) as excinfo:
        cli_main.main()

    assert excinfo.value.code == 1
    assert "--vid and --pid required" in capsys.readouterr().out


def test_cli_main_profiles_toggle_routes_to_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    from keyforge.cli import __main__ as cli_main

    calls: list[tuple[str, str]] = []

    def _set_profile_state(command: str, profile_name: str) -> None:
        calls.append((command, profile_name))

    monkeypatch.setattr(cli_main, "set_profile_state_cli", _set_profile_state)
    monkeypatch.setattr(sys, "argv", ["keyforge", "profiles", "toggle", "gaming"])

    cli_main.main()
    assert calls == [("toggle_profile", "gaming")]


def test_cli_main_devices_runs_async_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    from keyforge.cli import __main__ as cli_main

    called: list[bool] = []

    async def _list_devices(verbose: bool) -> None:
        called.append(verbose)

    monkeypatch.setattr(cli_main, "list_devices", _list_devices)
    monkeypatch.setattr(sys, "argv", ["keyforge", "devices", "--verbose"])

    cli_main.main()
    assert called == [True]
