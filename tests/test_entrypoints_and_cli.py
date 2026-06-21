import runpy
import sys
import types
from collections.abc import Callable

import pytest

from keymasq.common.security import SecurityPolicyError


def _module_with_main(fn: Callable[[], None]) -> types.ModuleType:
    module = types.ModuleType("stub_module")
    module.main = fn
    return module


def test_root_entrypoint_prefers_cli_when_args(monkeypatch: pytest.MonkeyPatch) -> None:
    from keymasq import __main__ as app_main

    called: list[str] = []
    monkeypatch.setitem(
        sys.modules,
        "keymasq.cli.__main__",
        _module_with_main(lambda: called.append("cli")),
    )
    monkeypatch.setitem(
        sys.modules,
        "keymasq.gui.__main__",
        _module_with_main(lambda: called.append("gui")),
    )
    monkeypatch.setattr(sys, "argv", ["keymasq", "profiles", "list"])

    app_main.main()
    assert called == ["cli"]


def test_root_entrypoint_uses_gui_with_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    from keymasq import __main__ as app_main

    called: list[str] = []
    monkeypatch.setitem(
        sys.modules,
        "keymasq.cli.__main__",
        _module_with_main(lambda: called.append("cli")),
    )
    monkeypatch.setitem(
        sys.modules,
        "keymasq.gui.__main__",
        _module_with_main(lambda: called.append("gui")),
    )
    monkeypatch.setattr(sys, "argv", ["keymasq"])
    monkeypatch.setenv("DISPLAY", ":0")

    app_main.main()
    assert called == ["gui"]


def test_root_entrypoint_falls_back_to_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    from keymasq import __main__ as app_main

    called: list[str] = []
    monkeypatch.setitem(
        sys.modules,
        "keymasq.cli.__main__",
        _module_with_main(lambda: called.append("cli")),
    )
    monkeypatch.setitem(
        sys.modules,
        "keymasq.gui.__main__",
        _module_with_main(lambda: called.append("gui")),
    )
    monkeypatch.setattr(sys, "argv", ["keymasq"])
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    monkeypatch.delenv("DESKTOP_SESSION", raising=False)

    app_main.main()
    assert called == ["cli"]


def test_cli_main_version_uses_package_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from keymasq.cli import __main__ as cli_main

    monkeypatch.setattr(cli_main, "__version__", "9.9.9")
    monkeypatch.setattr(sys, "argv", ["keymasq", "--version"])

    with pytest.raises(SystemExit) as excinfo:
        cli_main.main()

    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == "keymasq 9.9.9"


def test_cli_main_type_help_includes_inline_controls_and_docs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from keymasq.cli import __main__ as cli_main

    monkeypatch.setattr(cli_main, "__version__", "1.2.3")
    monkeypatch.setattr(sys, "argv", ["keymasq", "type", "--help"])

    with pytest.raises(SystemExit) as excinfo:
        cli_main.main()

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "<tab>, <enter>, <space>, <esc>, <backspace>, <delete>" in out
    assert "<KEY:COUNT>, <shortcut:MOD+KEY>, <move:X:Y>, <click[:X:Y]>" in out
    assert "<doubleclick[:X:Y]>, <rclick[:X:Y]>, <settle>, <wait:MS>, <wait:MIN:MAX>" in out
    assert "https://keymasq.tools/docs/v1.2.3/CLI.md" in out


def test_cli_main_play_help_includes_compact_tokens_and_docs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from keymasq.cli import __main__ as cli_main

    monkeypatch.setattr(cli_main, "__version__", "1.2.3")
    monkeypatch.setattr(sys, "argv", ["keymasq", "play", "--help"])

    with pytest.raises(SystemExit) as excinfo:
        cli_main.main()

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "move_abs:X:Y, move_rel:DX:DY, move:X:Y[:SPEED]," in out
    assert "wait:MS, wait:MIN:MAX" in out
    assert "https://keymasq.tools/docs/v1.2.3/CLI.md" in out


def test_keymasqd_script_entrypoint_calls_daemon_main(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setitem(
        sys.modules,
        "keymasq.keymasqd.daemon",
        _module_with_main(lambda: called.append("daemon")),
    )

    runpy.run_module("keymasq.keymasqd.__main__", run_name="__main__")
    assert called == ["daemon"]


def test_session_script_entrypoint_calls_manager_main(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setitem(
        sys.modules,
        "keymasq.session.manager",
        _module_with_main(lambda: called.append("manager")),
    )

    runpy.run_module("keymasq.session.__main__", run_name="__main__")
    assert called == ["manager"]


def test_session_entrypoint_delegates_help_to_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setitem(
        sys.modules,
        "keymasq.session.manager",
        _module_with_main(lambda: called.append("manager")),
    )
    monkeypatch.setattr(sys, "argv", ["keymasq-session", "--help"])

    runpy.run_module("keymasq.session.__main__", run_name="__main__")
    assert called == ["manager"]


def test_keymasqd_main_exits_on_security_policy_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from keymasq.keymasqd import daemon as daemon_module

    class _Daemon:
        def __init__(self, *, verbosity: int = 0) -> None:
            self.verbosity = verbosity

        async def start(self) -> None:
            raise SecurityPolicyError("Invalid security policy TOML at /tmp/security.toml: bad")

    monkeypatch.setattr(sys, "argv", ["keymasqd"])
    monkeypatch.setattr(daemon_module, "Daemon", _Daemon)
    monkeypatch.setattr(daemon_module.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(daemon_module, "ensure_uvloop", lambda _log: None)
    monkeypatch.setattr(daemon_module, "set_timer_slack_ns", lambda *, logger: None)

    with caplog.at_level("ERROR", logger="keymasqd"):
        with pytest.raises(SystemExit) as excinfo:
            daemon_module.main()

    assert excinfo.value.code == 1
    assert "Invalid security policy TOML at /tmp/security.toml: bad" in caplog.text


def test_session_main_exits_on_security_policy_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from keymasq.session.manager import core as manager_module

    class _SessionManager:
        def __init__(self, *, verbosity: int = 0) -> None:
            self.verbosity = verbosity
            raise SecurityPolicyError("Invalid security policy TOML at /tmp/security.toml: bad")

    monkeypatch.setattr(sys, "argv", ["keymasq-session"])
    monkeypatch.setattr(manager_module, "SessionManager", _SessionManager)
    monkeypatch.setattr(manager_module, "ensure_uvloop", lambda _log: None)

    with caplog.at_level("ERROR", logger="keymasq-session"):
        with pytest.raises(SystemExit) as excinfo:
            manager_module.main()

    assert excinfo.value.code == 1
    assert "Invalid security policy TOML at /tmp/security.toml: bad" in caplog.text


def test_cli_main_profiles_toggle_routes_to_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    from keymasq.cli import __main__ as cli_main
    from keymasq.cli import commands

    calls: list[tuple[str, str, bool]] = []

    def _set_profile_state(command: str, profile_name: str, *, json_output: bool) -> None:
        calls.append((command, profile_name, json_output))

    monkeypatch.setattr(commands, "set_profile_state_cli", _set_profile_state)
    monkeypatch.setattr(sys, "argv", ["keymasq", "profiles", "toggle", "gaming"])

    cli_main.main()
    assert calls == [("toggle_profile", "gaming", False)]


def test_cli_main_status_routes_json_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from keymasq.cli import __main__ as cli_main
    from keymasq.cli import commands

    calls: list[bool] = []

    def _status_cli(*, json_output: bool) -> None:
        calls.append(json_output)

    monkeypatch.setattr(commands, "status_cli", _status_cli)
    monkeypatch.setattr(sys, "argv", ["keymasq", "status", "--json"])

    cli_main.main()
    assert calls == [True]


def test_cli_main_mpris_routes_to_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    from keymasq.cli import __main__ as cli_main
    from keymasq.cli import commands

    calls: list[tuple[str, bool]] = []

    def _mpris_cli(command: str, *, json_output: bool) -> None:
        calls.append((command, json_output))

    monkeypatch.setattr(commands, "mpris_cli", _mpris_cli)
    monkeypatch.setattr(sys, "argv", ["keymasq", "mpris", "play-pause", "--json"])

    cli_main.main()
    assert calls == [("play-pause", True)]


def test_cli_main_mpris_status_routes_to_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    from keymasq.cli import __main__ as cli_main
    from keymasq.cli import commands

    calls: list[bool] = []

    def _mpris_status_cli(*, json_output: bool) -> None:
        calls.append(json_output)

    monkeypatch.setattr(commands, "mpris_status_cli", _mpris_status_cli)
    monkeypatch.setattr(sys, "argv", ["keymasq", "--json", "mpris", "status"])

    cli_main.main()
    assert calls == [True]


def test_cli_main_type_routes_to_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    from keymasq.cli import __main__ as cli_main
    from keymasq.cli import commands

    calls: list[dict[str, object]] = []

    def _type_cli(text: list[str], **kwargs: object) -> None:
        calls.append({"text": text, **kwargs})

    monkeypatch.setattr(commands, "type_cli", _type_cli)
    monkeypatch.setattr(
        sys,
        "argv",
        ["keymasq", "type", "--speed", "1.25", "--down-ms", "5", "hello"],
    )

    cli_main.main()
    assert calls == [
        {
            "text": ["hello"],
            "down_ms": 5,
            "pause_ms": 20,
            "speed": 1.25,
            "use_unicode_input": True,
            "print_json": False,
            "json_output": False,
        }
    ]


def test_cli_main_type_no_unicode_routes_to_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    from keymasq.cli import __main__ as cli_main
    from keymasq.cli import commands

    calls: list[dict[str, object]] = []

    def _type_cli(text: list[str], **kwargs: object) -> None:
        calls.append({"text": text, **kwargs})

    monkeypatch.setattr(commands, "type_cli", _type_cli)
    monkeypatch.setattr(sys, "argv", ["keymasq", "type", "--no-unicode", "café"])

    cli_main.main()
    assert calls[0]["use_unicode_input"] is False


def test_cli_main_play_routes_json_input_to_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    from keymasq.cli import __main__ as cli_main
    from keymasq.cli import commands

    calls: list[dict[str, object]] = []

    def _play_cli(events: list[str], **kwargs: object) -> None:
        calls.append({"events": events, **kwargs})

    monkeypatch.setattr(commands, "play_adhoc_cli", _play_cli)
    monkeypatch.setattr(
        sys,
        "argv",
        ["keymasq", "--json", "play", "--json", "--speed", "2", '{"events":[]}'],
    )

    cli_main.main()
    assert calls == [
        {
            "events": ['{"events":[]}'],
            "input_json": True,
            "speed": 2.0,
            "print_json": False,
            "json_output": True,
        }
    ]


def test_cli_main_play_omits_speed_override_when_not_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from keymasq.cli import __main__ as cli_main
    from keymasq.cli import commands

    calls: list[dict[str, object]] = []

    def _play_cli(events: list[str], **kwargs: object) -> None:
        calls.append({"events": events, **kwargs})

    monkeypatch.setattr(commands, "play_adhoc_cli", _play_cli)
    monkeypatch.setattr(sys, "argv", ["keymasq", "play", "--json", '{"events":[],"speed":2}'])

    cli_main.main()
    assert calls[0]["speed"] is None


@pytest.mark.parametrize(
    "argv",
    [
        ["keymasq", "type", "--speed", "nan", "hello"],
        ["keymasq", "type", "--speed", "inf", "hello"],
        ["keymasq", "play", "--speed", "nan", "key_a"],
        ["keymasq", "play", "--speed", "inf", "key_a"],
        ["keymasq", "macros", "play", "--speed", "nan", "stored"],
        ["keymasq", "macros", "play", "--speed", "inf", "stored"],
    ],
)
def test_cli_main_rejects_non_finite_speed_options(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from keymasq.cli import __main__ as cli_main

    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as excinfo:
        cli_main.main()

    assert excinfo.value.code == 2
    assert "must be a finite number greater than 0" in capsys.readouterr().err


@pytest.mark.parametrize("interval", ["-1", "0", "nan", "inf"])
def test_cli_main_rejects_invalid_diagnostics_interval(
    interval: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from keymasq.cli import __main__ as cli_main

    monkeypatch.setattr(sys, "argv", ["keymasq", "diagnostics", "on", "--interval", interval])

    with pytest.raises(SystemExit) as excinfo:
        cli_main.main()

    assert excinfo.value.code == 2
    assert "must be a finite number greater than 0" in capsys.readouterr().err


def test_cli_main_macros_create_routes_to_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    from keymasq.cli import __main__ as cli_main
    from keymasq.cli import commands

    calls: list[dict[str, object]] = []

    def _create_macro(name: str, json_parts: list[str], **kwargs: object) -> None:
        calls.append({"name": name, "json_parts": json_parts, **kwargs})

    monkeypatch.setattr(commands, "create_macro_cli", _create_macro)
    monkeypatch.setattr(
        sys,
        "argv",
        ["keymasq", "macros", "create", "--force", "stored", '{"events":[]}'],
    )

    cli_main.main()
    assert calls == [
        {
            "name": "stored",
            "json_parts": ['{"events":[]}'],
            "force": True,
            "json_output": False,
        }
    ]


def test_cli_main_macros_delete_routes_to_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    from keymasq.cli import __main__ as cli_main
    from keymasq.cli import commands

    calls: list[dict[str, object]] = []

    def _delete_macro(name: str, **kwargs: object) -> None:
        calls.append({"name": name, **kwargs})

    monkeypatch.setattr(commands, "delete_macro_cli", _delete_macro)
    monkeypatch.setattr(sys, "argv", ["keymasq", "macros", "delete", "stored"])

    cli_main.main()
    assert calls == [{"name": "stored", "json_output": False}]
