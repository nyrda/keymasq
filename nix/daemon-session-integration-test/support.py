#!/usr/bin/env python3

import concurrent.futures
import contextlib
import json
import os
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

import evdev

HARDWARE_ID = "cafe:0001"
SECOND_HARDWARE_ID = "cafe:0002"
SOURCE_NAME = "keymasq-integration-source-keyboard"
SECOND_SOURCE_NAME = "keymasq-integration-secondary-keyboard"
PROFILE_NAME = "Integration Core Smoke"
SECOND_PROFILE_NAME = "Integration Priority Override"
LOWER_PROFILE_NAME = "Integration Lower Fallback"
PASSTHROUGH_PROFILE_NAME = "Integration Passthrough Override"
MACRO_NAME = "integration-macro"
LONG_MACRO_NAME = "integration-hold-macro"
SUPERKEY_NAME = "integration-tap-superkey"
COMBO_SUPERKEY_NAME = "integration-combo-superkey"
OVERLOAD_SUPERKEY_NAME = "integration-overload-superkey"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@dataclass(frozen=True)
class ScenarioCase:
    name: str
    run: Callable[["ScenarioContext"], None]


class ScenarioContext:
    def __init__(self) -> None:
        runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        self.session_socket = runtime_dir / "keymasq" / "session.sock"
        self.config_dir = Path.home() / ".config" / "keymasq"
        self.source: evdev.UInput | None = None
        self.secondary_source: evdev.UInput | None = None
        self.keyboard_output: evdev.InputDevice | None = None
        self.mouse_output: evdev.InputDevice | None = None
        self.gamepad_output: evdev.InputDevice | None = None

    def setup(self) -> None:
        self.wait_for_session()
        self.wait_for_keymasqd_connection()
        self.source = self.create_source_keyboard(SOURCE_NAME, vendor=0xCAFE, product=0x0001)
        self.secondary_source = self.create_source_keyboard(
            SECOND_SOURCE_NAME,
            vendor=0xCAFE,
            product=0x0002,
        )
        self.write_configs(self.source.device.path, self.secondary_source.device.path)
        self.create_macro()
        self.request({"command": "reload"})
        self.request({"command": "reevaluate_hardware"})
        self.reopen_outputs()

    def cleanup(self) -> None:
        with contextlib.suppress(Exception):
            self.request(
                {"command": "disable_profile", "profile_name": PASSTHROUGH_PROFILE_NAME},
                ok=False,
            )
            self.request(
                {"command": "disable_profile", "profile_name": LOWER_PROFILE_NAME},
                ok=False,
            )
            self.request(
                {"command": "disable_profile", "profile_name": SECOND_PROFILE_NAME},
                ok=False,
            )
            self.request({"command": "disable_profile", "profile_name": PROFILE_NAME}, ok=False)
        for device in self.output_devices():
            device.close()
        if self.source is not None:
            self.source.close()
        if self.secondary_source is not None:
            self.secondary_source.close()

    def wait_for_session(self) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.session_socket.exists():
                try:
                    result = self.request({"command": "ping"}, timeout=1.0, ok=False)
                    if result.get("status") == "ok":
                        return
                except OSError:
                    pass
            time.sleep(0.25)
        raise AssertionError(f"session socket did not become ready: {self.session_socket}")

    def wait_for_keymasqd_connection(self) -> None:
        deadline = time.monotonic() + 60
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            try:
                last = self.request({"command": "get_status"}, timeout=2.0)
                if last.get("keymasqd_connected") is True:
                    return
            except OSError:
                pass
            time.sleep(0.5)
        raise AssertionError(f"keymasqd did not connect to session: {last}")

    def request(
        self,
        payload: dict[str, Any],
        *,
        timeout: float = 10.0,
        ok: bool = True,
    ) -> dict[str, Any]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(self.session_socket))
            client.sendall(json.dumps(payload).encode("utf-8") + b"\n")

            data = b""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                chunk = client.recv(4096)
                if not chunk:
                    break
                data += chunk
                while b"\n" in data:
                    line_bytes, data = data.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", "ignore")
                    if not line.strip():
                        continue
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        continue
                    if "event" in message and "status" not in message:
                        continue
                    if ok and message.get("status") not in {"ok", None}:
                        raise AssertionError(f"session request failed: {payload} -> {message}")
                    return message
        raise AssertionError(f"no response for session request: {payload}")

    def create_source_keyboard(self, name: str, *, vendor: int, product: int) -> evdev.UInput:
        source_keys = [
            getattr(evdev.ecodes, f"KEY_{letter}") for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        ]
        device = evdev.UInput(
            events={evdev.ecodes.EV_KEY: source_keys},
            name=name,
            vendor=vendor,
            product=product,
        )
        if not getattr(device.device, "path", None):
            device.close()
            raise AssertionError("source uinput did not expose an evdev path")
        self.settle_udev()
        time.sleep(0.5)
        return device

    def write_configs(self, primary_source_path: str, secondary_source_path: str) -> None:
        hardware_dir = self.config_dir / "hardware"
        profiles_dir = self.config_dir / "profiles"
        superkeys_dir = self.config_dir / "superkeys"
        for directory in (hardware_dir, profiles_dir, superkeys_dir):
            directory.mkdir(parents=True, exist_ok=True)

        values = self.fixture_values(primary_source_path, secondary_source_path)
        self.write_hardware_configs(primary_source_path, secondary_source_path)
        self.write_fixture(
            superkeys_dir / "integration-tap-superkey.toml",
            "superkeys/tap-superkey.toml",
            values,
        )
        self.write_fixture(
            superkeys_dir / "integration-combo-superkey.toml",
            "superkeys/combo-superkey.toml",
            values,
        )
        self.write_fixture(
            superkeys_dir / "integration-overload-superkey.toml",
            "superkeys/overload-superkey.toml",
            values,
        )
        self.write_fixture(
            profiles_dir / "integration-core-smoke.toml",
            "profiles/core-smoke.toml",
            values,
        )
        self.write_fixture(
            profiles_dir / "integration-priority-override.toml",
            "profiles/priority-override.toml",
            values,
        )
        self.write_fixture(
            profiles_dir / "integration-lower-fallback.toml",
            "profiles/lower-fallback.toml",
            values,
        )
        self.write_fixture(
            profiles_dir / "integration-passthrough-override.toml",
            "profiles/passthrough-override.toml",
            values,
        )

    def write_hardware_configs(
        self,
        primary_source_path: str,
        secondary_source_path: str,
    ) -> None:
        hardware_dir = self.config_dir / "hardware"
        hardware_dir.mkdir(parents=True, exist_ok=True)
        values = self.fixture_values(primary_source_path, secondary_source_path)
        self.write_fixture(hardware_dir / "cafe_0001.toml", "hardware/primary.toml", values)
        self.write_fixture(hardware_dir / "cafe_0002.toml", "hardware/secondary.toml", values)

    def write_fixture(self, destination: Path, fixture_name: str, values: dict[str, str]) -> None:
        template = Template((FIXTURES_DIR / fixture_name).read_text(encoding="utf-8"))
        destination.write_text(template.safe_substitute(values), encoding="utf-8")

    def fixture_values(
        self,
        primary_source_path: str,
        secondary_source_path: str,
    ) -> dict[str, str]:
        return {
            "HARDWARE_ID": HARDWARE_ID,
            "SECOND_HARDWARE_ID": SECOND_HARDWARE_ID,
            "PRIMARY_SOURCE_PATH": primary_source_path,
            "SECONDARY_SOURCE_PATH": secondary_source_path,
            "PRIMARY_BUTTONS": self.button_blocks("abcdefghijklmnopqrstuvwxyz"),
            "SECONDARY_BUTTONS": self.button_blocks("abcdefghijklmnopqrstuvwxyz"),
            "PROFILE_NAME": PROFILE_NAME,
            "SECOND_PROFILE_NAME": SECOND_PROFILE_NAME,
            "LOWER_PROFILE_NAME": LOWER_PROFILE_NAME,
            "PASSTHROUGH_PROFILE_NAME": PASSTHROUGH_PROFILE_NAME,
            "MACRO_NAME": MACRO_NAME,
            "LONG_MACRO_NAME": LONG_MACRO_NAME,
            "SUPERKEY_NAME": SUPERKEY_NAME,
            "COMBO_SUPERKEY_NAME": COMBO_SUPERKEY_NAME,
            "OVERLOAD_SUPERKEY_NAME": OVERLOAD_SUPERKEY_NAME,
        }

    def button_blocks(self, keys: str) -> str:
        return "\n\n".join(
            f"""
[[hardware.layout.buttons]]
id = "key_{key}"
label = "{key.upper()}"
evdev = "key_{key}"
source = "kbd"
type = "key"
""".strip()
            for key in keys
        )

    def create_macro(self) -> None:
        self.create_macro_named(
            MACRO_NAME,
            [
                {
                    "device_type": "keyboard",
                    "type": evdev.ecodes.EV_KEY,
                    "code": evdev.ecodes.KEY_Y,
                    "value": 1,
                    "t_us": 0,
                },
                {
                    "device_type": "keyboard",
                    "type": evdev.ecodes.EV_KEY,
                    "code": evdev.ecodes.KEY_Y,
                    "value": 0,
                    "t_us": 10000,
                },
            ],
        )
        self.create_macro_named(
            LONG_MACRO_NAME,
            [
                {
                    "device_type": "keyboard",
                    "type": evdev.ecodes.EV_KEY,
                    "code": evdev.ecodes.KEY_Y,
                    "value": 1,
                    "t_us": 0,
                },
                {
                    "device_type": "keyboard",
                    "type": evdev.ecodes.EV_KEY,
                    "code": evdev.ecodes.KEY_Y,
                    "value": 0,
                    "t_us": 5_000_000,
                },
            ],
        )

    def create_macro_named(self, name: str, events: list[dict[str, object]]) -> None:
        result = self.request(
            {
                "command": "create_macro",
                "macro": {
                    "name": name,
                    "events": events,
                },
            },
            ok=False,
        )
        if result.get("status") == "ok":
            return
        if "already exists" in str(result.get("message", "")).lower():
            return
        raise AssertionError(f"macro creation failed: {result}")

    def request_background(
        self,
        payload: dict[str, Any],
        *,
        timeout: float = 20.0,
    ) -> tuple[concurrent.futures.Executor, concurrent.futures.Future[dict[str, Any]]]:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(lambda: self.request(payload, timeout=timeout))
        return executor, future

    def restart_session(self) -> None:
        subprocess.run(
            [
                os.environ.get("KEYMASQ_INTEGRATION_SYSTEMCTL", "systemctl"),
                "--user",
                "restart",
                "keymasq-session.service",
            ],
            check=True,
            timeout=30,
        )
        self.wait_for_session()
        self.wait_for_keymasqd_connection()
        self.request({"command": "reevaluate_hardware"})
        self.reopen_outputs()

    def restart_keymasqd(self) -> None:
        subprocess.run(
            [
                os.environ.get("KEYMASQ_INTEGRATION_SUDO", "sudo"),
                os.environ.get("KEYMASQ_INTEGRATION_SYSTEMCTL", "systemctl"),
                "restart",
                "keymasqd.service",
            ],
            check=True,
            timeout=30,
        )
        self.wait_for_session()
        self.wait_for_keymasqd_connection()
        self.request({"command": "reevaluate_hardware"})
        self.reopen_outputs()

    def recreate_secondary_source(self) -> None:
        if self.source is None:
            raise AssertionError("primary source keyboard is not available")
        if self.secondary_source is not None:
            self.secondary_source.close()
            self.secondary_source = None
        self.settle_udev()
        time.sleep(0.5)
        self.secondary_source = self.create_source_keyboard(
            SECOND_SOURCE_NAME,
            vendor=0xCAFE,
            product=0x0002,
        )
        self.write_hardware_configs(self.source.device.path, self.secondary_source.device.path)
        self.request({"command": "reload"})
        self.request({"command": "reevaluate_hardware"})
        self.reopen_outputs()

    def reopen_outputs(self) -> None:
        self.close_outputs()
        self.keyboard_output = self.wait_for_output_device(
            {"keymasq-test-keyboard", "keymasq-keyboard"},
            "keyboard",
        )
        self.mouse_output = self.wait_for_output_device(
            {"keymasq-test-mouse", "keymasq-mouse"},
            "mouse",
        )
        self.gamepad_output = self.wait_for_output_device(
            {"keymasq-test-gamepad", "keymasq-gamepad"},
            "gamepad",
        )
        self.drain_outputs()

    def close_outputs(self) -> None:
        for device in self.output_devices():
            device.close()
        self.keyboard_output = None
        self.mouse_output = None
        self.gamepad_output = None

    def wait_for_output_device(self, wanted: set[str], label: str) -> evdev.InputDevice:
        deadline = time.monotonic() + 30
        last_status: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            self.settle_udev()
            for path in evdev.list_devices():
                try:
                    device = evdev.InputDevice(path)
                except OSError:
                    continue
                if device.name in wanted:
                    return device
                device.close()
            last_status = self.request({"command": "get_active_profiles"}, ok=False)
            time.sleep(0.5)
        raise AssertionError(f"output {label} was not created; active profiles: {last_status}")

    def open_passthrough_output(self, hardware_id: str) -> evdev.InputDevice:
        return self.wait_for_output_device(
            {f"keymasq-test-passthrough-{hardware_id}", f"keymasq-{hardware_id}"},
            f"passthrough {hardware_id}",
        )

    def subtest(self, label: str, fn: Callable[[], object]) -> None:
        print(f"integration: {label}", flush=True)
        self.drain_outputs()
        fn()

    def tap_source(self, code: int, *, pause_s: float = 0.03) -> None:
        self.source_key(code, 1)
        time.sleep(pause_s)
        self.source_key(code, 0)

    def tap_secondary_source(self, code: int, *, pause_s: float = 0.03) -> None:
        self.secondary_key(code, 1)
        time.sleep(pause_s)
        self.secondary_key(code, 0)

    def source_key(self, code: int, value: int) -> None:
        if self.source is None:
            raise AssertionError("source keyboard is not available")
        self.source.write(evdev.ecodes.EV_KEY, code, value)
        self.source.syn()

    def secondary_key(self, code: int, value: int) -> None:
        if self.secondary_source is None:
            raise AssertionError("secondary source keyboard is not available")
        self.secondary_source.write(evdev.ecodes.EV_KEY, code, value)
        self.secondary_source.syn()

    def expect_keys(self, expected: list[tuple[int, int]], *, timeout_s: float = 3.0) -> None:
        self.expect_events(
            self.keyboard_output,
            [(evdev.ecodes.EV_KEY, code, value) for code, value in expected],
            label="keyboard",
            timeout_s=timeout_s,
        )

    def expect_mouse_events(
        self,
        expected: list[tuple[int, int, int]],
        *,
        timeout_s: float = 3.0,
    ) -> None:
        self.expect_events(self.mouse_output, expected, label="mouse", timeout_s=timeout_s)

    def expect_gamepad_events(
        self,
        expected: list[tuple[int, int, int]],
        *,
        timeout_s: float = 3.0,
    ) -> None:
        self.expect_events(self.gamepad_output, expected, label="gamepad", timeout_s=timeout_s)

    def expect_events(
        self,
        device: evdev.InputDevice | None,
        expected: list[tuple[int, int, int]],
        *,
        label: str,
        timeout_s: float = 3.0,
    ) -> None:
        if device is None:
            raise AssertionError(f"output {label} is not available")

        observed: list[tuple[int, int, int]] = []
        index = 0
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for event in self.read_output_events(device):
                if event.type == evdev.ecodes.EV_SYN:
                    continue
                event_tuple = (int(event.type), int(event.code), int(event.value))
                observed.append(event_tuple)
                if event_tuple == expected[index]:
                    index += 1
                    if index == len(expected):
                        return
            time.sleep(0.01)

        expected_names = [self.event_label(event) for event in expected]
        observed_names = [self.event_label(event) for event in observed]
        raise AssertionError(
            f"missing {label} output sequence {expected_names}; observed {observed_names}"
        )

    def expect_no_keyboard_events(self, *, timeout_s: float = 0.25) -> None:
        self.expect_no_events(
            self.keyboard_output,
            label="keyboard",
            event_types={evdev.ecodes.EV_KEY},
            timeout_s=timeout_s,
        )

    def expect_no_mouse_events(self, *, timeout_s: float = 0.25) -> None:
        self.expect_no_events(
            self.mouse_output,
            label="mouse",
            event_types={evdev.ecodes.EV_KEY, evdev.ecodes.EV_REL},
            timeout_s=timeout_s,
        )

    def expect_no_gamepad_events(self, *, timeout_s: float = 0.25) -> None:
        self.expect_no_events(
            self.gamepad_output,
            label="gamepad",
            event_types={evdev.ecodes.EV_KEY, evdev.ecodes.EV_ABS},
            timeout_s=timeout_s,
        )

    def expect_no_events(
        self,
        device: evdev.InputDevice | None,
        *,
        label: str,
        event_types: set[int],
        timeout_s: float = 0.25,
    ) -> None:
        if device is None:
            raise AssertionError(f"output {label} is not available")
        observed: list[tuple[int, int, int]] = []
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for event in self.read_output_events(device):
                event_tuple = (int(event.type), int(event.code), int(event.value))
                if event.type in event_types:
                    observed.append(event_tuple)
            time.sleep(0.01)
        if observed:
            observed_names = [self.event_label(event) for event in observed]
            raise AssertionError(f"unexpected {label} output events: {observed_names}")

    def wait_for_active_profile(self, profile_name: str, *, enabled: bool) -> None:
        deadline = time.monotonic() + 5
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            last = self.request({"command": "get_active_profiles"}, ok=False)
            active = set(str(name) for name in last.get("active_profiles", []))
            if (profile_name in active) is enabled:
                return
            time.sleep(0.1)
        raise AssertionError(f"profile {profile_name} enabled={enabled} not observed: {last}")

    def set_profile_enabled(self, profile_name: str, *, enabled: bool) -> None:
        command = "enable_profile" if enabled else "disable_profile"
        self.request({"command": command, "profile_name": profile_name})
        self.wait_for_active_profile(profile_name, enabled=enabled)

    def wait_until(
        self,
        label: str,
        predicate: Callable[[], bool],
        *,
        timeout_s: float = 5,
    ) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        raise AssertionError(f"timed out waiting for {label}")

    def output_devices(self) -> list[evdev.InputDevice]:
        return [
            device
            for device in (self.keyboard_output, self.mouse_output, self.gamepad_output)
            if device is not None
        ]

    def drain_outputs(self) -> None:
        end = time.monotonic() + 0.05
        while time.monotonic() < end:
            events = [
                event
                for device in self.output_devices()
                for event in self.read_output_events(device)
            ]
            if not events:
                time.sleep(0.005)

    def read_output_events(self, device: evdev.InputDevice) -> list[evdev.InputEvent]:
        try:
            return list(device.read())
        except BlockingIOError:
            return []
        except OSError:
            return []

    def event_label(self, event: tuple[int, int, int]) -> str:
        event_type, code, value = event
        if event_type == evdev.ecodes.EV_KEY:
            name = evdev.ecodes.KEY.get(code, str(code))
        elif event_type == evdev.ecodes.EV_REL:
            name = evdev.ecodes.REL.get(code, str(code))
        elif event_type == evdev.ecodes.EV_ABS:
            name = evdev.ecodes.ABS.get(code, str(code))
        else:
            name = str(code)
        return f"{event_type}:{name}:{value}"

    def settle_udev(self) -> None:
        try:
            subprocess.run(
                ["udevadm", "settle", "--timeout=5"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=6,
            )
        except Exception:
            pass
