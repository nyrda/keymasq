import os
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_SCRIPT = REPOSITORY_ROOT / "scripts" / "integration.sh"
DAEMON_INTEGRATION_ROOT = REPOSITORY_ROOT / "nix" / "daemon-session-integration-test"

EXPECTED_CHECKS = {
    "daemon-session-integration-test",
    "listener-vm-gnome-bridge",
    "listener-vm-gnome",
    "listener-vm-kde",
    "listener-vm-hyprland",
    "listener-vm-niri",
    "listener-vm-xfce",
    "listener-vm-cosmic",
    "listener-vm-sway",
}


def _fake_nix(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "nix-calls.log"
    executable = bin_dir / "nix"
    executable.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$KEYMASQ_TEST_NIX_LOG"
if [[ "${KEYMASQ_TEST_REBUILD_MISSING:-0}" == "1" && "$*" == *"--rebuild"* ]]; then
  echo "error: some outputs are not valid, so checking is not possible" >&2
  exit 1
fi
"""
    )
    executable.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["KEYMASQ_TEST_NIX_LOG"] = str(log_path)
    return env, log_path


def _run_script(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    env, log_path = _fake_nix(tmp_path)
    result = subprocess.run(
        ["bash", str(INTEGRATION_SCRIPT), *args],
        cwd=REPOSITORY_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    calls = log_path.read_text().splitlines() if log_path.exists() else []
    return result, calls


def test_all_integration_checks_are_rebuilt_and_logged(tmp_path: Path) -> None:
    result, calls = _run_script(tmp_path, "all")

    assert result.returncode == 0, result.stderr
    assert len(calls) == len(EXPECTED_CHECKS)
    assert {
        call.rsplit(".", 1)[-1]
        for call in calls
    } == EXPECTED_CHECKS
    assert all("build --no-link --print-build-logs --rebuild" in call for call in calls)


def test_missing_rebuild_output_falls_back_to_a_fresh_logged_build(tmp_path: Path) -> None:
    env, log_path = _fake_nix(tmp_path)
    env["KEYMASQ_TEST_REBUILD_MISSING"] = "1"

    result = subprocess.run(
        ["bash", str(INTEGRATION_SCRIPT), "cosmic"],
        cwd=REPOSITORY_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    calls = log_path.read_text().splitlines()
    assert len(calls) == 2
    assert "--rebuild" in calls[0]
    assert "--rebuild" not in calls[1]
    assert "--print-build-logs" in calls[1]


def test_daemon_scenario_registry_has_unique_selectable_keys_and_full_module_coverage() -> None:
    verification = r"""
from pathlib import Path

from runner import _scenario_key, selected_scenarios
from scenarios import SCENARIOS

keys = [_scenario_key(scenario.name) for scenario in SCENARIOS]
assert len(SCENARIOS) == 47
assert len(keys) == len(set(keys))
assert all(key and key.replace('-', '').isalnum() for key in keys)
assert _scenario_key('simple 1->1 remap') == 'simple-1-1-remap'
assert _scenario_key('superkey overload multi-action press/release') == (
    'superkey-overload-multi-action-press-release'
)
assert [case.name for case in selected_scenarios(['simple-1-1-remap'])] == [
    'simple 1->1 remap'
]

registered_modules = {scenario.run.__module__.rsplit('.', 1)[-1] for scenario in SCENARIOS}
scenario_modules = {
    path.stem
    for path in (Path.cwd() / 'scenarios').glob('*.py')
    if path.stem not in {'__init__', 'profile_lifetime_helpers'}
}
assert registered_modules == scenario_modules
"""
    result = subprocess.run(
        [sys.executable, "-c", verification],
        cwd=DAEMON_INTEGRATION_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_runner_lists_all_registered_scenarios_without_starting_context() -> None:
    result = subprocess.run(
        [sys.executable, "runner.py", "--list"],
        cwd=DAEMON_INTEGRATION_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 47
    assert "simple-1-1-remap\tsimple 1->1 remap" in lines
    assert (
        "superkey-overload-multi-action-press-release\t"
        "superkey overload multi-action press/release"
    ) in lines
