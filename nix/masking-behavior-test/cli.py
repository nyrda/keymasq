import json
import os
import pty
import select
import subprocess
import sys
import time

from behavior import gui_client, snapshot, wait_for, wait_state
from control import NAMES, assert_masked, devices, mask_state, restored, restored_device
from support import ScenarioContext

CLI = os.environ["KEYMASQ_CLI"]
ENV = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
PROMPT = b"Does your input still work?"
PAUSED = "Off \N{MIDDLE DOT} remapping stopped by recovery"


def run(*args):
    return subprocess.run(
        [CLI, "masking", *args],
        env=ENV,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def cli(*args, status=0):
    result = run(*args)
    output = result.stdout + result.stderr
    assert result.returncode == status, (args, result.returncode, output)
    return output


def cli_json(*args, status=0):
    result = run(*args, "--json")
    assert result.returncode == status, (args, result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout)


def read_terminal(fd, until=None):
    data = b""
    deadline = time.monotonic() + 120
    while until is None or until not in data:
        assert time.monotonic() < deadline, data
        if not select.select([fd], [], [], 0.1)[0]:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            chunk = b""
        if not chunk:
            assert until is None, data
            break
        data += chunk
    return data


def enable_in_terminal(selector, answer, at_prompt):
    master, terminal = pty.openpty()
    process = subprocess.Popen(
        [CLI, "masking", "enable", selector],
        env=ENV,
        stdin=terminal,
        stdout=terminal,
        stderr=terminal,
    )
    os.close(terminal)
    try:
        output = read_terminal(master, PROMPT)
        at_prompt()
        os.write(master, answer + b"\n")
        output += read_terminal(master)
        return process.wait(timeout=120), output.decode()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


def awaiting_confirmation(name):
    def check():
        state = mask_state(ScenarioContext(), devices()[name].identity)
        assert state["state"] == "trial" and not state.get("automatic"), state
        assert_masked(name, forwarding=True, check_bystander=False)

    return check


def rows(listing):
    return {item["id"]: item for item in listing["devices"]}


def selectors():
    ctx = ScenarioContext()
    target, bystander = (devices()[name] for name in NAMES)
    before = snapshot(ctx)
    listing = cli_json("list")
    assert listing["available"] and not listing["remapping_suspended"], listing
    for item in (target, bystander):
        row = rows(listing)[item.identity]
        assert (row["name"], row["transport"], row["connection"]) == (
            item.name,
            item.transport,
            item.kernel_name,
        ), row
        assert row["summary"].startswith("Off"), row
        assert row["supported"] and not row["mask"]["enabled"], row
    lines = cli("list").splitlines()
    assert lines[0].split() == ["ID", "NAME", "TRANSPORT", "CONNECTION", "STATUS"], lines
    for item in (target, bystander):
        assert [item.identity[:12], item.name, item.transport, item.kernel_name, "Off"] in [
            line.split()[:5] for line in lines
        ], lines
    for selector in (
        target.identity,
        target.identity[:8],
        target.name.upper(),
        target.kernel_name,
        target.kernel_name.lower(),
    ):
        assert cli_json("show", selector)["device"]["id"] == target.identity, selector
    shown = cli("show", bystander.identity[:6]).splitlines()
    assert shown[0] == bystander.name, shown
    assert f"  id: {bystander.identity}" in shown, shown
    assert any(line.startswith("  status: Off") for line in shown), shown
    assert f"  connection: {bystander.kernel_name}" in shown, shown
    model = f"{target.vendor}:{target.product}"
    assert model == f"{bystander.vendor}:{bystander.product}", (target, bystander)
    for args in (("show", model), ("enable", model.upper(), "--yes"), ("disable", model)):
        output = cli(*args, status=2)
        assert "matches several devices" in output, output
        assert target.identity[:12] in output and bystander.identity[:12] in output, output
    assert cli_json("show", model, status=2)["status"] == "error"
    for selector in ("no-such-device", target.identity[:3]):
        assert "No device matches" in cli("show", selector, status=2)
    assert snapshot(ctx) == before


def prompt_no():
    target = devices()[NAMES[0]]
    status, output = enable_in_terminal(target.name, b"n", awaiting_confirmation(NAMES[0]))
    assert status == 1 and "Masking was undone" in output, (status, output)
    state = mask_state(ScenarioContext(), target.identity)
    assert state["state"] == "restored" and not state["enabled"], state
    assert not state["has_saved_mask"], state
    restored()


def failed():
    target = devices()[NAMES[0]]
    output = cli("enable", target.name, "--yes", status=1)
    assert "Error: " in output, output
    state = mask_state(ScenarioContext(), target.identity)
    assert state.get("error") and state["state"] != "masked", state
    assert not state["has_saved_mask"], state
    assert "is not waiting for confirmation" in cli("confirm", target.identity, status=1)
    assert cli_json("show", target.name)["device"]["mask"]["failure"]


def failure_recovered():
    ctx = ScenarioContext()
    target = devices()[NAMES[0]]
    state = wait_state(ctx, target.identity, "restored")
    assert not state["enabled"] and not state["has_saved_mask"], state
    assert not ctx.request({"command": "hardware_inventory"})["remapping_suspended"]
    restored()


def no_prompt():
    ctx = ScenarioContext()
    target = devices()[NAMES[0]]
    output = cli("enable", target.identity[:8], "--no-prompt")
    assert f"Run 'keymasq masking confirm {target.identity[:12]}' within" in output, output
    awaiting_confirmation(NAMES[0])()
    assert not mask_state(ctx, target.identity)["has_saved_mask"]
    assert cli("confirm", target.identity).strip() == f"{target.name}: Masked"
    state = mask_state(ctx, target.identity)
    assert state["state"] == "masked" and state["persist"] and state["has_saved_mask"], state
    assert_masked(forwarding=True)
    assert "is not waiting for confirmation" in cli("confirm", target.name, status=1)
    assert cli("disable", target.name).strip() == f"{target.name}: Off"
    state = mask_state(ctx, target.identity)
    assert not state["enabled"] and not state["persist"], state
    restored()


def prompt_yes():
    bystander = devices()[NAMES[1]]
    status, output = enable_in_terminal(
        bystander.kernel_name, b"y", awaiting_confirmation(NAMES[1])
    )
    assert status == 0 and f"{bystander.name}: Masked" in output, (status, output)
    state = mask_state(ScenarioContext(), bystander.identity)
    assert state["state"] == "masked" and state["persist"] and not state.get("automatic"), state
    assert_masked(NAMES[1], forwarding=True, check_bystander=False)
    restored_device(NAMES[0])


def pause_resume():
    ctx = ScenarioContext()
    target, bystander = (devices()[name] for name in NAMES)
    output = cli("enable", target.name)
    assert "confirm" not in output, output
    state = mask_state(ctx, target.identity)
    assert state["state"] == "masked" and state["automatic"] and state["persist"], state
    for name in NAMES:
        assert_masked(name, forwarding=True, check_bystander=False)
    with gui_client() as client:
        client.request({"command": "restore_hardware"})
    listing = cli_json("list")
    assert listing["remapping_suspended"], listing
    for item in (target, bystander):
        row = rows(listing)[item.identity]
        assert row["summary"] == PAUSED and not row["mask"]["enabled"], row
    assert "Run 'keymasq masking resume'" in cli("list")
    restored()
    output = cli("enable", target.name, "--yes", status=1)
    assert "Resume remapping" in output, output
    restored()
    assert cli("disable", bystander.name).strip() == f"{bystander.name}: {PAUSED}"
    assert not mask_state(ctx, bystander.identity)["persist"]
    assert cli("resume").strip() == "Remapping resumed"
    state = wait_state(ctx, target.identity, "masked")
    assert state["automatic"] and state["persist"], state
    assert not ctx.request({"command": "hardware_inventory"})["remapping_suspended"]
    assert_masked(forwarding=True, check_bystander=False)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        assert not mask_state(ctx, bystander.identity)["enabled"]
        time.sleep(0.1)
    restored_device(NAMES[1])
    assert cli("disable", "--all").strip() == "Masking turned off for all devices"
    restored()
    for item in (target, bystander):
        state = mask_state(ctx, item.identity)
        assert not state["enabled"] and not state["persist"], state
    assert "--no-wait cannot be used with --all" in cli("disable", "--all", "--no-wait", status=2)


def usb_yes():
    ctx = ScenarioContext()
    target, bystander = (devices()[name] for name in NAMES)
    assert bystander.transport == "usb", bystander
    before = snapshot(ctx)
    output = cli("enable", f"{bystander.vendor}:{bystander.product}", "--yes", status=2)
    assert "matches several devices" in output, output
    assert snapshot(ctx) == before
    output = cli("enable", bystander.kernel_name, "--yes")
    assert "Does your input" not in output and f"{bystander.name}: Masked" in output, output
    state = mask_state(ctx, bystander.identity)
    assert state["state"] == "masked" and state["persist"] and not state.get("automatic"), state
    assert_masked(NAMES[1], forwarding=True, check_bystander=False)
    restored_device(NAMES[0])
    assert cli("disable", bystander.identity[:6]).strip() == f"{bystander.name}: Off"
    state = mask_state(ctx, bystander.identity)
    assert not state["enabled"] and not state["persist"], state
    restored()


def model():
    composite = wait_for(
        "composite USB attachment",
        lambda: devices(require_all=False),
        lambda found: NAMES[0] in found,
    )[NAMES[0]]
    shown = cli_json("show", f"{composite.vendor}:{composite.product}")["device"]
    assert (shown["id"], shown["transport"], shown["connection"]) == (
        composite.identity,
        "usb",
        composite.kernel_name,
    ), shown


COMMANDS = {
    "selectors": selectors,
    "prompt-no": prompt_no,
    "failed": failed,
    "failure-recovered": failure_recovered,
    "no-prompt": no_prompt,
    "prompt-yes": prompt_yes,
    "pause-resume": pause_resume,
    "usb-yes": usb_yes,
    "model": model,
}

if __name__ == "__main__":
    ScenarioContext().assert_daemon_has_no_capabilities()
    COMMANDS[sys.argv[1]]()
    ScenarioContext().assert_daemon_has_no_capabilities()
