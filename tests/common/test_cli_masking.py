import json
import sys
from types import SimpleNamespace

import pytest

from keymasq.cli import __main__ as cli_main
from keymasq.cli import masking


def device(identity: str, **fields) -> dict:
    return {
        "id": identity,
        "generation": "gen",
        "name": "Controller",
        "vendor": "28de",
        "product": "1205",
        "transport": "usb",
        "connection": "3-1",
        "supported": True,
        **fields,
    }


class FakeSession:
    def __init__(self, devices: list[dict], masks: list[dict]) -> None:
        self.devices = devices
        self.masks = {mask["id"]: mask for mask in masks}
        self.requests: list[dict] = []

    def __call__(self, payload: dict, timeout: float = 5.0) -> dict:
        assert timeout >= masking.HARDWARE_GUI_TIMEOUT
        self.requests.append(payload)
        identity = payload.get("id")
        mask = self.masks.get(identity, {"id": identity})
        if payload["command"] == "mask_hardware":
            self.masks[identity] = {
                "id": identity,
                "state": "trial",
                "token": "trial-token",
                "remaining_seconds": 30,
            }
        elif payload["command"] == "keep_hardware_mask":
            self.masks[identity] = {**mask, "state": "masked", "persist": True}
        elif payload["command"] == "restore_hardware":
            self.masks[identity] = {
                **mask,
                "state": "restored",
                "enabled": False,
                "reason": "user_restore",
            }
        return {
            "status": "ok",
            "available": True,
            "devices": self.devices,
            "masks": list(self.masks.values()),
        }

    def commands(self) -> list[dict]:
        return [request for request in self.requests if request["command"] != "hardware_inventory"]


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch):
    def install(devices: list[dict], masks: list[dict] | None = None) -> FakeSession:
        fake = FakeSession(devices, masks or [])
        monkeypatch.setattr(masking, "_session_request", fake)
        return fake

    return install


ENTRIES = [
    (device("aaaa1111bbbb2222cccc3333", name="Pad", connection="3-1"), {}),
    (device("dddd4444eeee5555ffff6666", name="Twin", connection="5-1", product="1142"), {}),
    (device("dddd7777eeee8888ffff9999", name="Twin", connection="5-2", product="1142"), {}),
]


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("aaaa1111bbbb2222cccc3333", "aaaa1111bbbb2222cccc3333"),
        ("AAAA11", "aaaa1111bbbb2222cccc3333"),
        ("dddd7", "dddd7777eeee8888ffff9999"),
        ("5-1", "dddd4444eeee5555ffff6666"),
        ("28de:1205", "aaaa1111bbbb2222cccc3333"),
        ("  pad ", "aaaa1111bbbb2222cccc3333"),
    ],
)
def test_selector_resolves_one_device(selector: str, expected: str) -> None:
    device_entry, _mask = masking.resolve_device(ENTRIES, selector)
    assert device_entry["id"] == expected


@pytest.mark.parametrize(
    ("selector", "message"),
    [
        ("Twin", "matches several devices"),
        ("28de:1142", "matches several devices"),
        ("dddd", "matches several devices"),
        ("aaa", "No device matches"),
        ("", "Name a device"),
    ],
)
def test_selector_never_guesses(selector: str, message: str) -> None:
    with pytest.raises(masking.MaskingError, match=message) as raised:
        masking.resolve_device(ENTRIES, selector)
    assert raised.value.exit_code == masking.SELECTOR_EXIT_CODE


def interactive(monkeypatch: pytest.MonkeyPatch, *, answer: str | None, tty: bool = True) -> None:
    stdin = SimpleNamespace(isatty=lambda: tty, readline=lambda: f"{answer}\n")

    def wait_for_answer(readers, _writers, _errors, timeout):
        assert 0 < timeout < 30
        if answer is None:
            raise KeyboardInterrupt
        return (readers if answer else [], [], [])

    monkeypatch.setattr(masking.sys, "stdin", stdin)
    monkeypatch.setattr(masking.select, "select", wait_for_answer)


@pytest.mark.parametrize(
    ("answer", "command", "exit_code"),
    [
        ("y", "keep_hardware_mask", None),
        ("no", "restore_hardware", 1),
        ("", "restore_hardware", 1),
        (None, "restore_hardware", 130),
    ],
)
def test_first_enable_keeps_only_after_explicit_yes(
    session, monkeypatch, capsys, answer, command, exit_code
) -> None:
    fake = session([device("pad")])
    interactive(monkeypatch, answer=answer)
    if exit_code is None:
        masking.enable_cli("pad")
    else:
        with pytest.raises(SystemExit) as raised:
            masking.enable_cli("pad")
        assert raised.value.code == exit_code
    assert fake.commands() == [
        {"command": "mask_hardware", "id": "pad", "persist": True, "generation": "gen"},
        {
            "command": command,
            "id": "pad",
            "token": "trial-token",
            "persist": command != "restore_hardware",
        },
    ]
    assert "Does your input still work?" in capsys.readouterr().out


@pytest.mark.parametrize(("tty", "confirmation"), [(False, "ask"), (True, "later")])
def test_enable_without_prompt_leaves_trial_for_confirm(
    session, monkeypatch, capsys, tty, confirmation
) -> None:
    fake = session([device("aaaa1111bbbb2222cccc3333")])
    interactive(monkeypatch, answer=None, tty=tty)
    masking.enable_cli("aaaa11", confirmation=confirmation)
    assert [request["command"] for request in fake.commands()] == ["mask_hardware"]
    assert "Run 'keymasq masking confirm aaaa1111bbbb' within 30s" in capsys.readouterr().out
    masking.confirm_cli("aaaa11")
    assert fake.commands()[-1] == {
        "command": "keep_hardware_mask",
        "id": "aaaa1111bbbb2222cccc3333",
        "token": "trial-token",
        "persist": True,
    }


@pytest.mark.parametrize("json_output", [False, True])
def test_enable_yes_keeps_trial_without_asking(session, monkeypatch, json_output) -> None:
    fake = session([device("pad")])
    interactive(monkeypatch, answer=None)
    masking.enable_cli("pad", confirmation="yes", json_output=json_output)
    assert fake.commands()[-1] == {
        "command": "keep_hardware_mask",
        "id": "pad",
        "token": "trial-token",
        "persist": True,
    }


def test_enable_saved_device_while_unplugged_waits_without_prompt(session, monkeypatch) -> None:
    saved = {"id": "pad", "state": "restored", "token": "old", "has_saved_mask": True}
    fake = session([device("pad", supported=False)], [saved])

    def mask_saved(payload: dict, timeout: float = 5.0) -> dict:
        if payload["command"] == "mask_hardware":
            fake.requests.append(payload)
            fake.masks["pad"] = {**saved, "enabled": True, "persist": True}
            return {"status": "ok"}
        return fake(payload, timeout)

    monkeypatch.setattr(masking, "_session_request", mask_saved)
    interactive(monkeypatch, answer=None)
    masking.enable_cli("pad")
    assert fake.commands() == [{"command": "mask_hardware", "id": "pad", "persist": True}]


@pytest.mark.parametrize("json_output", [False, True])
def test_confirm_fails_when_mask_does_not_survive_keep(
    session, monkeypatch, capsys, json_output
) -> None:
    trial = {"id": "pad", "state": "trial", "token": "trial-token", "remaining_seconds": 30}
    fake = session([device("pad")], [trial])

    def keep_then_fail(payload: dict, timeout: float = 5.0) -> dict:
        result = fake(payload, timeout)
        if payload["command"] == "keep_hardware_mask":
            fake.masks["pad"] = {**trial, "state": "recovery_failed"}
        return result

    monkeypatch.setattr(masking, "_session_request", keep_then_fail)
    with pytest.raises(SystemExit) as raised:
        masking.confirm_cli("pad", json_output=json_output)
    assert raised.value.code == 1
    out = capsys.readouterr().out
    assert "could not be restored" in out
    if json_output:
        assert json.loads(out)["status"] == "error"


@pytest.mark.parametrize(
    ("mask", "token"),
    [
        ({"id": "pad", "state": "masked", "token": "live", "enabled": True}, "live"),
        ({"id": "pad", "state": "restored", "token": "saved", "enabled": True}, "saved"),
        ({"id": "pad", "state": "restored", "token": "old", "enabled": False}, None),
        (None, None),
    ],
)
def test_disable_restores_only_devices_that_are_on(session, mask, token) -> None:
    connected = mask is None or mask["state"] == "masked"
    fake = session([device("pad", supported=connected)], [mask] if mask else [])
    masking.disable_cli("pad")
    assert fake.commands() == (
        [{"command": "restore_hardware", "id": "pad", "token": token, "persist": False}]
        if token
        else []
    )


def test_json_list_merges_masks_without_internal_fields(session, capsys) -> None:
    session(
        [device("pad"), device("gone", supported=False, attachment_path="/sys/x", token="t")],
        [
            {
                "id": "pad",
                "state": "trial",
                "token": "secret",
                "usb_selector": {},
                "remaining_seconds": 12,
            },
            {
                "id": "gone",
                "state": "restored",
                "token": "t2",
                "enabled": True,
                "has_saved_mask": True,
            },
        ],
    )
    masking.list_cli(json_output=True)
    output = capsys.readouterr().out
    assert "secret" not in output and "usb_selector" not in output and "/sys/x" not in output
    result = json.loads(output)
    pad, gone = result["devices"]
    assert pad["summary"] == "Masked · awaiting confirmation"
    assert pad["mask"]["state"] == "trial" and pad["mask"]["remaining_seconds"] == 12
    assert gone["summary"] == "Waiting for device"
    assert gone["mask"]["enabled"] is True


@pytest.mark.parametrize(
    ("argv", "function", "args", "kwargs"),
    [
        (["masking", "list"], "list_cli", (), {"json_output": False}),
        (["--json", "masking", "show", "pad"], "show_cli", ("pad",), {"json_output": True}),
        (
            ["masking", "enable", "pad"],
            "enable_cli",
            ("pad",),
            {"confirmation": "ask", "json_output": False},
        ),
        (
            ["masking", "enable", "pad", "-y"],
            "enable_cli",
            ("pad",),
            {"confirmation": "yes", "json_output": False},
        ),
        (
            ["masking", "enable", "pad", "--no-prompt"],
            "enable_cli",
            ("pad",),
            {"confirmation": "later", "json_output": False},
        ),
        (["masking", "confirm", "pad", "--json"], "confirm_cli", ("pad",), {"json_output": True}),
        (
            ["masking", "disable", "--all"],
            "disable_cli",
            (None,),
            {"wait": True, "json_output": False},
        ),
        (["masking", "resume"], "resume_cli", (), {"json_output": False}),
    ],
)
def test_masking_subcommands_dispatch(monkeypatch, argv, function, args, kwargs) -> None:
    calls = []
    monkeypatch.setattr(masking, function, lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(sys, "argv", ["keymasq", *argv])
    cli_main.main()
    assert calls == [(args, kwargs)]
