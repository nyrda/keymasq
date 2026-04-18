#!/usr/bin/env python3
# ruff: noqa: E402
"""
Macro replay fidelity benchmark for Keymasq.

This is an exploratory benchmark, not a correctness test. It measures how
closely the current Python macro engine reproduces a synthetic macro timeline
when its own output is recorded back through evdev.

Usage:
    nix develop -c python benchmarks/macro_replay_fidelity.py
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import importlib.util
import statistics
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import evdev

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from keymasq.keymasqd.device_manager import DeviceManager
from keymasq.keymasqd.recording import RecordingManager

PR_SET_TIMERSLACK = 29
PR_GET_TIMERSLACK = 30


@dataclass
class Scenario:
    name: str
    description: str
    macro_events: list[dict[str, object]]
    expected_devices: set[str]


@dataclass
class ScenarioResult:
    scenario: Scenario
    expected_count: int
    observed_count: int
    sequence_match: bool
    first_mismatch_index: int | None
    duration_expected_us: int
    duration_observed_us: int
    duration_error_us: int
    gap_error_mean_us: float
    gap_error_median_us: float
    gap_error_p95_us: float
    gap_error_p99_us: float
    gap_error_max_us: float
    abs_gap_error_mean_us: float
    abs_gap_error_median_us: float
    abs_gap_error_p95_us: float
    abs_gap_error_p99_us: float
    abs_gap_error_max_us: float
    collapsed_gap_count: int


def _calc_quantile(samples: list[float], q: float) -> float:
    if not samples:
        return 0.0
    if len(samples) == 1:
        return float(samples[0])
    sorted_samples = sorted(samples)
    index = min(len(sorted_samples) - 1, max(0, int(round((len(sorted_samples) - 1) * q))))
    return float(sorted_samples[index])


def _calc_int_quantile(samples: list[int], q: float) -> float:
    return _calc_quantile([float(value) for value in samples], q)


def _expected_event_signature(event: dict[str, object]) -> tuple[str, int, int, int]:
    return (
        str(event.get("device_type", "other")),
        int(event.get("type", 0)),
        int(event.get("code", 0)),
        int(event.get("value", 0)),
    )


def _observed_event_signature(event: dict[str, object]) -> tuple[str, int, int, int]:
    return (
        str(event.get("device_type", "other")),
        int(event.get("type", 0)),
        int(event.get("code", 0)),
        int(event.get("value", 0)),
    )


def _event_time_us(event: dict[str, object]) -> int:
    value = event.get("t_us", 0)
    return int(value) if isinstance(value, int) else 0


def _sort_key(event: dict[str, object]) -> tuple[int, str, int, int, int]:
    return (
        _event_time_us(event),
        str(event.get("device_type", "other")),
        int(event.get("type", 0)),
        int(event.get("code", 0)),
        int(event.get("value", 0)),
    )


def _wait_for_device_path(name: str, timeout_s: float = 2.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for path in evdev.list_devices():
            dev = evdev.InputDevice(path)
            try:
                if dev.name == name:
                    return path
            finally:
                dev.close()
        time.sleep(0.02)
    raise RuntimeError(f"Timed out waiting for uinput device {name!r}")


def get_timer_slack_ns() -> int | None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.prctl(PR_GET_TIMERSLACK, 0, 0, 0, 0)
        if result < 0:
            return None
        return int(result)
    except Exception:
        return None


def set_timer_slack_ns(slack_ns: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.prctl(PR_SET_TIMERSLACK, ctypes.c_ulong(slack_ns), 0, 0, 0)
    if result != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"prctl(PR_SET_TIMERSLACK, {slack_ns}) failed")


class OutputDevices:
    def __init__(self) -> None:
        unique = uuid.uuid4().hex[:8]
        self.keyboard_name = f"benchmark-macro-keyboard-{unique}"
        self.mouse_name = f"benchmark-macro-mouse-{unique}"
        self.keyboard_uinput = evdev.UInput(
            events={
                evdev.ecodes.EV_KEY: [
                    evdev.ecodes.KEY_A,
                    evdev.ecodes.KEY_B,
                    evdev.ecodes.KEY_C,
                    evdev.ecodes.KEY_LEFTCTRL,
                    evdev.ecodes.KEY_LEFTSHIFT,
                    evdev.ecodes.KEY_SPACE,
                ],
                evdev.ecodes.EV_SYN: [],
            },
            name=self.keyboard_name,
        )
        self.mouse_uinput = evdev.UInput(
            events={
                evdev.ecodes.EV_KEY: [
                    evdev.ecodes.BTN_LEFT,
                    evdev.ecodes.BTN_RIGHT,
                    evdev.ecodes.BTN_MIDDLE,
                ],
                evdev.ecodes.EV_REL: [
                    evdev.ecodes.REL_X,
                    evdev.ecodes.REL_Y,
                    evdev.ecodes.REL_WHEEL,
                ],
                evdev.ecodes.EV_SYN: [],
            },
            name=self.mouse_name,
        )
        self.keyboard_path = _wait_for_device_path(self.keyboard_name)
        self.mouse_path = _wait_for_device_path(self.mouse_name)

    def close(self) -> None:
        self.keyboard_uinput.close()
        self.mouse_uinput.close()


def _mouse_click_scenario(name: str, cps: int, clicks: int) -> Scenario:
    period_us = int(round(1_000_000 / cps))
    half_period_us = max(1, period_us // 2)
    events: list[dict[str, object]] = []
    for index in range(clicks):
        start = index * period_us
        events.append(
            {
                "t_us": start,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.BTN_LEFT,
                "value": 1,
                "device_type": "mouse",
            }
        )
        events.append(
            {
                "t_us": start + half_period_us,
                "type": evdev.ecodes.EV_KEY,
                "code": evdev.ecodes.BTN_LEFT,
                "value": 0,
                "device_type": "mouse",
            }
        )
    return Scenario(
        name=name,
        description=f"{clicks} left-clicks at {cps} CPS ({period_us} us period)",
        macro_events=events,
        expected_devices={"mouse"},
    )


def _keyboard_burst_scenario() -> Scenario:
    events: list[dict[str, object]] = []
    codes = [
        evdev.ecodes.KEY_A,
        evdev.ecodes.KEY_B,
        evdev.ecodes.KEY_C,
        evdev.ecodes.KEY_SPACE,
    ]
    t_us = 0
    for index in range(80):
        code = codes[index % len(codes)]
        events.append(
            {
                "t_us": t_us,
                "type": evdev.ecodes.EV_KEY,
                "code": code,
                "value": 1,
                "device_type": "keyboard",
            }
        )
        t_us += 750
        events.append(
            {
                "t_us": t_us,
                "type": evdev.ecodes.EV_KEY,
                "code": code,
                "value": 0,
                "device_type": "keyboard",
            }
        )
        t_us += 750
    return Scenario(
        name="keyboard_667hz_burst",
        description="80 keyboard press/release pairs at 750 us half-period",
        macro_events=events,
        expected_devices={"keyboard"},
    )


def _mixed_dense_scenario() -> Scenario:
    events: list[dict[str, object]] = []
    timeline = [
        (0, "keyboard", evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 1),
        (400, "keyboard", evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1),
        (800, "keyboard", evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0),
        (1_200, "keyboard", evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTCTRL, 0),
        (1_500, "mouse", evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 3),
        (1_800, "mouse", evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, -2),
        (2_100, "mouse", evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 1),
        (2_700, "mouse", evdev.ecodes.EV_KEY, evdev.ecodes.BTN_LEFT, 0),
        (3_000, "keyboard", evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTSHIFT, 1),
        (3_500, "keyboard", evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 1),
        (3_900, "keyboard", evdev.ecodes.EV_KEY, evdev.ecodes.KEY_B, 0),
        (4_300, "keyboard", evdev.ecodes.EV_KEY, evdev.ecodes.KEY_LEFTSHIFT, 0),
        (4_700, "mouse", evdev.ecodes.EV_REL, evdev.ecodes.REL_WHEEL, 1),
    ]
    repeat_gap_us = 5_000
    for repeat in range(60):
        offset = repeat * repeat_gap_us
        for t_us, device_type, event_type, code, value in timeline:
            events.append(
                {
                    "t_us": offset + t_us,
                    "type": event_type,
                    "code": code,
                    "value": value,
                    "device_type": device_type,
                }
            )
    return Scenario(
        name="mixed_dense_sequence",
        description="60 repeats of an irregular mixed keyboard/mouse sequence",
        macro_events=events,
        expected_devices={"keyboard", "mouse"},
    )


def build_scenarios() -> list[Scenario]:
    return [
        _mouse_click_scenario("mouse_click_250cps", cps=250, clicks=150),
        _mouse_click_scenario("mouse_click_500cps", cps=500, clicks=150),
        _mouse_click_scenario("mouse_click_1000cps", cps=1000, clicks=150),
        _mouse_click_scenario("mouse_click_2000cps", cps=2000, clicks=150),
        _keyboard_burst_scenario(),
        _mixed_dense_scenario(),
    ]


async def _wait_for_macro_completion(manager: DeviceManager, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while manager.macro_state.tasks:
        if time.monotonic() >= deadline:
            raise TimeoutError("Timed out waiting for macro playback to finish")
        await asyncio.sleep(0.001)


async def _wait_for_recording_settle(
    recorder: RecordingManager,
    expected_count: int,
    *,
    stable_s: float = 0.05,
    timeout_s: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout_s
    last_count = len(recorder._events)  # type: ignore[attr-defined]
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        await asyncio.sleep(0.005)
        current = len(recorder._events)  # type: ignore[attr-defined]
        if current != last_count:
            last_count = current
            last_change = time.monotonic()
            continue
        if current >= expected_count and (time.monotonic() - last_change) >= stable_s:
            return
        if current < expected_count and (time.monotonic() - last_change) >= stable_s:
            return


def _build_recording_devices(
    output_devices: OutputDevices, devices: set[str]
) -> list[dict[str, object]]:
    configs: list[dict[str, object]] = []
    if "keyboard" in devices:
        configs.append(
            {
                "path": output_devices.keyboard_path,
                "device_type": "keyboard",
                "device_types": ["keyboard"],
            }
        )
    if "mouse" in devices:
        configs.append(
            {
                "path": output_devices.mouse_path,
                "device_type": "mouse",
                "device_types": ["mouse"],
            }
        )
    return configs


def _compare_scenario(
    scenario: Scenario,
    observed_events: list[dict[str, object]],
) -> ScenarioResult:
    expected_events = sorted((dict(event) for event in scenario.macro_events), key=_sort_key)
    observed_sorted = sorted((dict(event) for event in observed_events), key=_sort_key)
    expected_count = len(expected_events)
    observed_count = len(observed_sorted)
    sequence_match = True
    first_mismatch_index: int | None = None
    pairs = zip(expected_events, observed_sorted, strict=False)
    for index, (expected, observed) in enumerate(pairs):
        if _expected_event_signature(expected) != _observed_event_signature(observed):
            sequence_match = False
            first_mismatch_index = index
            break
    if sequence_match and expected_count != observed_count:
        sequence_match = False
        first_mismatch_index = min(expected_count, observed_count)

    comparable_count = min(expected_count, observed_count)
    signed_gap_errors: list[int] = []
    abs_gap_errors: list[int] = []
    collapsed_gap_count = 0
    for index in range(1, comparable_count):
        expected_gap = _event_time_us(expected_events[index]) - _event_time_us(
            expected_events[index - 1]
        )
        observed_gap = _event_time_us(observed_sorted[index]) - _event_time_us(
            observed_sorted[index - 1]
        )
        signed_gap_errors.append(observed_gap - expected_gap)
        abs_gap_errors.append(abs(observed_gap - expected_gap))
        if expected_gap > 0 and observed_gap == 0:
            collapsed_gap_count += 1

    duration_expected_us = _event_time_us(expected_events[-1]) if expected_events else 0
    duration_observed_us = max((_event_time_us(event) for event in observed_sorted), default=0)

    return ScenarioResult(
        scenario=scenario,
        expected_count=expected_count,
        observed_count=observed_count,
        sequence_match=sequence_match,
        first_mismatch_index=first_mismatch_index,
        duration_expected_us=duration_expected_us,
        duration_observed_us=duration_observed_us,
        duration_error_us=duration_observed_us - duration_expected_us,
        gap_error_mean_us=statistics.mean(signed_gap_errors) if signed_gap_errors else 0.0,
        gap_error_median_us=statistics.median(signed_gap_errors) if signed_gap_errors else 0.0,
        gap_error_p95_us=_calc_int_quantile(signed_gap_errors, 0.95),
        gap_error_p99_us=_calc_int_quantile(signed_gap_errors, 0.99),
        gap_error_max_us=float(max(signed_gap_errors, key=abs)) if signed_gap_errors else 0.0,
        abs_gap_error_mean_us=statistics.mean(abs_gap_errors) if abs_gap_errors else 0.0,
        abs_gap_error_median_us=statistics.median(abs_gap_errors) if abs_gap_errors else 0.0,
        abs_gap_error_p95_us=_calc_int_quantile(abs_gap_errors, 0.95),
        abs_gap_error_p99_us=_calc_int_quantile(abs_gap_errors, 0.99),
        abs_gap_error_max_us=float(max(abs_gap_errors)) if abs_gap_errors else 0.0,
        collapsed_gap_count=collapsed_gap_count,
    )


async def run_scenario(output_devices: OutputDevices, scenario: Scenario) -> ScenarioResult:
    manager = DeviceManager()
    manager.output_state.keyboard_uinput = output_devices.keyboard_uinput
    manager.output_state.mouse_uinput = output_devices.mouse_uinput

    recorder = RecordingManager()
    devices = _build_recording_devices(output_devices, scenario.expected_devices)
    await recorder.start(
        devices,
        include_mouse_movement=True,
        include_mouse_clicks=True,
    )
    await asyncio.sleep(0.1)
    try:
        result = await manager.play_macro(
            macro_events=scenario.macro_events,
            macro_name=scenario.name,
            replay_mouse_movement=True,
            replay_mouse_clicks=True,
            speed=1.0,
            loop_mode="none",
            loop_count=1,
            move_to_start=False,
            start_x=0,
            start_y=0,
            block_mouse_movement=False,
            source_device="benchmark",
            source_button=scenario.name,
            trigger_value=1,
        )
        if result.get("status") != "ok":
            raise RuntimeError(f"Macro playback failed for {scenario.name}: {result}")
        await _wait_for_macro_completion(manager)
        await _wait_for_recording_settle(recorder, len(scenario.macro_events))
    finally:
        payload = await recorder.stop()
        await manager.cancel_macro_playback()

    observed_events = [dict(event) for event in payload["events"]]  # type: ignore[index]
    return _compare_scenario(scenario, observed_events)


def print_result(result: ScenarioResult) -> None:
    print(f"{result.scenario.name}")
    print(f"  Description:         {result.scenario.description}")
    print(
        "  Events:              "
        f"expected={result.expected_count} observed={result.observed_count}"
    )
    print(f"  Sequence match:      {'yes' if result.sequence_match else 'no'}")
    if result.first_mismatch_index is not None:
        print(f"  First mismatch:      index {result.first_mismatch_index}")
    print(
        "  Duration:            "
        f"expected={result.duration_expected_us} us "
        f"observed={result.duration_observed_us} us "
        f"error={result.duration_error_us:+d} us"
    )
    print(
        "  Gap error signed:    "
        f"mean={result.gap_error_mean_us:.1f} us "
        f"median={result.gap_error_median_us:.1f} us "
        f"p95={result.gap_error_p95_us:.1f} us "
        f"p99={result.gap_error_p99_us:.1f} us "
        f"max={result.gap_error_max_us:.1f} us"
    )
    print(
        "  Gap error abs:       "
        f"mean={result.abs_gap_error_mean_us:.1f} us "
        f"median={result.abs_gap_error_median_us:.1f} us "
        f"p95={result.abs_gap_error_p95_us:.1f} us "
        f"p99={result.abs_gap_error_p99_us:.1f} us "
        f"max={result.abs_gap_error_max_us:.1f} us"
    )
    print(f"  Collapsed gaps:      {result.collapsed_gap_count}")
    print()


async def async_main(selected: set[str]) -> int:
    scenarios = [
        scenario
        for scenario in build_scenarios()
        if not selected or scenario.name in selected
    ]
    if not scenarios:
        print("No scenarios selected", file=sys.stderr)
        return 1

    output_devices = OutputDevices()
    try:
        for scenario in scenarios:
            result = await run_scenario(output_devices, scenario)
            print_result(result)
    finally:
        output_devices.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark macro replay fidelity")
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="Run only the named scenario. Can be passed multiple times.",
    )
    parser.add_argument(
        "--uvloop",
        action="store_true",
        help="Use uvloop as the asyncio event loop policy for this benchmark run.",
    )
    parser.add_argument(
        "--timerslack-ns",
        type=int,
        default=None,
        help="Set per-process timer slack in nanoseconds for this benchmark run.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.uvloop:
        if importlib.util.find_spec("uvloop") is None:
            print("uvloop requested but not installed in this environment", file=sys.stderr)
            return 2
        import uvloop

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    original_slack_ns = get_timer_slack_ns()
    if args.timerslack_ns is not None:
        set_timer_slack_ns(args.timerslack_ns)
    active_loop = "uvloop" if args.uvloop else "asyncio-default"
    current_slack_ns = get_timer_slack_ns()
    print(
        f"# macro_replay_fidelity loop={active_loop} "
        f"timer_slack_ns={current_slack_ns} "
        f"original_timer_slack_ns={original_slack_ns}"
    )
    return asyncio.run(async_main(set(args.scenario)))


if __name__ == "__main__":
    raise SystemExit(main())
