#!/usr/bin/env python3
"""
Grabbed-input pipeline cost and event-loop stall benchmark for keymasqd.

Measures what the evdev microbenchmarks cannot see:
1. CPU time of the real grabbed-device pipeline per input frame
2. The longest event-loop block while profile changes are applied
3. Input wakeup delay while a worker thread runs Python code
4. Full garbage collection cost of the daemon's import-time heap

No root or uinput access is needed; outputs are discarded in memory.

Usage:
    nix develop -c python benchmarks/pipeline_latency.py
"""

import argparse
import asyncio
import gc
import importlib
import os
import statistics
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import evdev  # noqa: E402
import pytest  # noqa: E402

from keymasq.common.model.actions import MappingAction  # noqa: E402
from keymasq.common.model.core import ActionType  # noqa: E402
from keymasq.keymasqd.device_manager import DeviceManager  # noqa: E402
from keymasq.keymasqd.runtime.grabbed_device.device import GrabbedDevice  # noqa: E402
from keymasq.keymasqd.runtime.grabbed_device.event import pipeline  # noqa: E402
from tests.keymasqd.device_manager_support import (  # noqa: E402
    grabbed_event_processing_deps,
    make_combo_grabbed_device,
    make_combo_runtime_setup,
)

E = evdev.ecodes
KEYBOARD_KEYS = [f"key_{c}" for c in "abcdefghijklmnopqrstuvwxyz"] + [
    f"key_f{i}" for i in range(1, 25)
]

GENERATOR = r"""
import os, struct, sys, time
fd = int(sys.argv[1])
end = time.perf_counter_ns() + int(float(sys.argv[2]) * 1e9)
due = time.perf_counter_ns()
while (now := time.perf_counter_ns()) < end:
    if now >= due:
        os.write(fd, struct.pack("q", now))
        due += 1_000_000
    else:
        time.sleep(max(0.0, (due - now) / 1e9 - 0.0002))
"""


class NullUInput:
    def write(self, *_args: object) -> None:
        return

    def syn(self) -> None:
        return

    def close(self) -> None:
        return


def event(event_type: int, code: int, value: int) -> evdev.InputEvent:
    return evdev.InputEvent(0, 0, event_type, code, value)


def describe(values: Sequence[float]) -> str:
    ordered = sorted(values)
    count = len(ordered)
    return (
        f"median {statistics.median(ordered):8.1f} us  "
        f"p99 {ordered[int(count * 0.99)]:8.1f} us  "
        f"p99.9 {ordered[int(count * 0.999)]:8.1f} us  "
        f"max {ordered[-1]:8.1f} us"
    )


def isolate_masking_state(monkeypatch: pytest.MonkeyPatch) -> None:
    state = Path(tempfile.mkdtemp(prefix="keymasq-bench-"))
    monkeypatch.setattr("keymasq.masking.backend.STATE_DIR", state / "state")
    monkeypatch.setattr("keymasq.masking.paths.POLICY_DIR", state / "policy")


async def bench_pipeline(monkeypatch: pytest.MonkeyPatch, frames: int) -> None:
    print("\n== Pipeline CPU time per input frame (no syscalls)")
    mapping = {"key_b": MappingAction(action_type=ActionType.KEYBOARD, target="key_c")}
    setup = await make_combo_runtime_setup(
        monkeypatch,
        [],
        button_map={"key_a": "key_a", "key_b": "key_b"},
        mapping=mapping,
        passthrough_uinput=NullUInput(),  # type: ignore[arg-type]
        keyboard_uinput=NullUInput(),  # type: ignore[arg-type]
    )
    unmapped = await make_combo_runtime_setup(
        monkeypatch,
        [],
        button_map={"key_a": "key_a"},
        passthrough_uinput=NullUInput(),  # type: ignore[arg-type]
        keyboard_uinput=NullUInput(),  # type: ignore[arg-type]
    )
    mouse = [event(E.EV_REL, E.REL_X, 3), event(E.EV_REL, E.REL_Y, -2), event(E.EV_SYN, 0, 0)]
    scenarios: list[tuple[str, GrabbedDevice, list[list[evdev.InputEvent]]]] = [
        ("mouse move, device without mappings", unmapped.device, [mouse]),
        ("mouse move, profile with mappings", setup.device, [mouse]),
        (
            "unmapped key press + release",
            setup.device,
            [
                [event(E.EV_KEY, E.KEY_A, 1), event(E.EV_SYN, 0, 0)],
                [event(E.EV_KEY, E.KEY_A, 0), event(E.EV_SYN, 0, 0)],
            ],
        ),
        (
            "remapped key press + release",
            setup.device,
            [
                [event(E.EV_KEY, E.KEY_B, 1), event(E.EV_SYN, 0, 0)],
                [event(E.EV_KEY, E.KEY_B, 0), event(E.EV_SYN, 0, 0)],
            ],
        ),
    ]
    deps = grabbed_event_processing_deps()
    for label, device, frame_cycle in scenarios:
        samples: list[float] = []
        for index in range(frames + 2000):
            for frame in frame_cycle:
                started = time.perf_counter_ns()
                for item in frame:
                    await pipeline.process_event(device, item, deps=deps)
                if index >= 2000:
                    samples.append((time.perf_counter_ns() - started) / 1000)
            if index % 16 == 0:
                await asyncio.sleep(0)
        events_per_frame = sum(len(frame) for frame in frame_cycle) / len(frame_cycle)
        per_event = statistics.median(samples) / events_per_frame
        print(f"  {label:38s} {describe(samples)}  (~{per_event:.1f} us/event)")


async def longest_block(operation: Callable[[], Awaitable[object]]) -> tuple[float, float]:
    gaps: list[float] = []
    done = asyncio.Event()

    async def monitor() -> None:
        last = time.perf_counter_ns()
        while not done.is_set():
            await asyncio.sleep(0)
            now = time.perf_counter_ns()
            gaps.append((now - last) / 1000)
            last = now

    task = asyncio.create_task(monitor())
    await asyncio.sleep(0)
    started = time.perf_counter_ns()
    await operation()
    total = (time.perf_counter_ns() - started) / 1000
    done.set()
    await task
    return total, max(gaps)


def keyboard_mapping(keys: int, superkeys: int) -> dict[str, object]:
    mapping: dict[str, object] = {}
    for index, key in enumerate(KEYBOARD_KEYS[:keys]):
        if index < superkeys:
            mapping[key] = {
                "action": "superkey",
                "superkey": {
                    "name": f"sk{index}",
                    "mode": "pattern",
                    "tap_actions": [{"action": "keyboard", "target": "key_a"}],
                    "hold_actions": [{"action": "keyboard", "target": "key_leftctrl"}],
                },
            }
        else:
            mapping[key] = {"action": "keyboard", "target": "key_x"}
    return mapping


def alt_combos(count: int) -> list[object]:
    return [
        {
            "id": f"combo-{index}",
            "name": f"combo-{index}",
            "steps": [
                {
                    "events": [
                        {"hardware_id": "kbd", "source": "kbd", "evdev": "key_leftalt"},
                        {"hardware_id": "kbd", "source": "kbd", "evdev": KEYBOARD_KEYS[index % 26]},
                    ]
                }
            ],
            "action": {"action": "keyboard", "target": "key_f13"},
        }
        for index in range(count)
    ]


async def bench_profile_changes(monkeypatch: pytest.MonkeyPatch, repeats: int) -> None:
    print("\n== Longest event-loop block while a profile change is applied")
    manager = DeviceManager()
    make_combo_grabbed_device(
        monkeypatch,
        manager,
        button_map={key: key for key in KEYBOARD_KEYS},
        hardware_id="kbd",
        passthrough_uinput=NullUInput(),
        keyboard_uinput=NullUInput(),
    )
    cases: list[tuple[str, Callable[[], Awaitable[object]]]] = []
    for keys, superkeys in ((len(KEYBOARD_KEYS), 0), (len(KEYBOARD_KEYS), 20)):
        mapping = keyboard_mapping(keys, superkeys)
        cases.append(
            (
                f"set_mapping {keys} keys, {superkeys} superkeys",
                lambda mapping=mapping: manager.set_mapping("kbd", mapping),
            )
        )
    for count in (10, 40):
        combos = alt_combos(count)
        cases.append(
            (f"set_combos {count} combos", lambda combos=combos: manager.set_combos(combos))
        )
    for label, operation in cases:
        await operation()
        results = [await longest_block(operation) for _ in range(repeats)]
        total = statistics.median(result[0] for result in results)
        block = statistics.median(result[1] for result in results)
        print(f"  {label:38s} total {total:7.0f} us  longest block {block:7.0f} us")


async def wakeup_delays(worker: Callable[[], object] | None, seconds: float) -> list[float]:
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    generator = subprocess.Popen(
        [sys.executable, "-c", GENERATOR, str(write_fd), str(seconds)],
        pass_fds=(write_fd,),
    )
    os.close(write_fd)
    loop = asyncio.get_running_loop()
    delays: list[float] = []
    finished = asyncio.Event()

    def on_readable() -> None:
        now = time.perf_counter_ns()
        try:
            data = os.read(read_fd, 4096)
        except BlockingIOError:
            return
        if not data:
            loop.remove_reader(read_fd)
            finished.set()
            return
        for (stamp,) in struct.iter_unpack("q", data[: len(data) // 8 * 8]):
            delays.append((now - stamp) / 1000)

    loop.add_reader(read_fd, on_readable)
    stop = False

    async def churn() -> None:
        while worker is not None and not stop:
            await asyncio.to_thread(worker)

    task = asyncio.create_task(churn())
    await finished.wait()
    stop = True
    await task
    generator.wait()
    os.close(read_fd)
    return delays


def python_work() -> None:
    total = 0
    for value in range(2_000_000):
        total += value & 7


async def bench_gil(seconds: float) -> None:
    print("\n== Input wakeup delay at 1 kHz while a worker thread runs Python")
    original = sys.getswitchinterval()
    try:
        for label, interval, worker in (
            ("idle, no worker", original, None),
            (f"worker, switch interval {original * 1000:g} ms", original, python_work),
            ("worker, switch interval 0.2 ms (keymasqd)", 0.0002, python_work),
        ):
            sys.setswitchinterval(interval)
            delays = await wakeup_delays(worker, seconds)
            late = sum(delay > 1000 for delay in delays)
            print(f"  {label:42s} {describe(delays)}  >1ms {late}")
    finally:
        sys.setswitchinterval(original)


def bench_gc() -> None:
    print("\n== Full garbage collection of the daemon's import-time heap")
    importlib.import_module("keymasq.keymasqd.daemon")

    def full_collection_ms() -> float:
        started = time.perf_counter_ns()
        gc.collect()
        return (time.perf_counter_ns() - started) / 1e6

    gc.collect()
    tracked = len(gc.get_objects())
    before = min(full_collection_ms() for _ in range(5))
    gc.freeze()
    try:
        after = min(full_collection_ms() for _ in range(5))
    finally:
        gc.unfreeze()
    print(f"  {tracked} tracked objects: {before:.2f} ms, after gc.freeze(): {after:.2f} ms")


def describe_environment() -> None:
    governor = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    print(f"python {sys.version.split()[0]}")
    if governor.exists():
        print(f"cpu governor {governor.read_text().strip()}, load {os.getloadavg()[0]:.2f}")


async def run(args: argparse.Namespace) -> None:
    print(f"event loop {type(asyncio.get_running_loop()).__module__}")
    monkeypatch = pytest.MonkeyPatch()
    try:
        isolate_masking_state(monkeypatch)
        await bench_pipeline(monkeypatch, args.frames)
        await bench_profile_changes(monkeypatch, args.repeats)
        await bench_gil(args.seconds)
    finally:
        monkeypatch.undo()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark keymasqd pipeline cost and stalls")
    parser.add_argument("--frames", type=int, default=20000, help="frames per pipeline scenario")
    parser.add_argument("--repeats", type=int, default=20, help="runs per profile change")
    parser.add_argument("--seconds", type=float, default=3.0, help="duration per wakeup test")
    parser.add_argument("--uvloop", action="store_true", help="run on uvloop like keymasqd")
    args = parser.parse_args(argv)
    if args.uvloop:
        import uvloop

        uvloop.install()
    describe_environment()
    bench_gc()
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
