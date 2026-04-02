#!/usr/bin/env python3
"""
Evdev passthrough latency benchmark for Keyforge.

Measures the overhead of Python evdev passthrough by:
1. Creating a virtual uinput source device
2. Reading events via evdev
3. Writing to another uinput device (simulating keyforge passthrough)
4. Measuring end-to-end latency

Usage:
    sudo python benchmarks/evdev_latency.py
"""

import argparse
import asyncio
import importlib.util
import os
import statistics
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import evdev

HAS_PYEVTEST = importlib.util.find_spec("pyevtest") is not None


@dataclass
class BenchmarkResult:
    name: str
    samples: int
    mean_us: float
    median_us: float
    min_us: float
    max_us: float
    p99_us: float
    stddev_us: float

    def __str__(self) -> str:
        return (
            f"{self.name}:\n"
            f"  Samples: {self.samples}\n"
            f"  Mean:   {self.mean_us:7.2f} us\n"
            f"  Median: {self.median_us:7.2f} us\n"
            f"  Min:    {self.min_us:7.2f} us\n"
            f"  Max:    {self.max_us:7.2f} us\n"
            f"  P99:    {self.p99_us:7.2f} us\n"
            f"  StdDev: {self.stddev_us:7.2f} us"
        )


def calc_stats(name: str, samples: list[float]) -> BenchmarkResult:
    if not samples:
        return BenchmarkResult(name, 0, 0, 0, 0, 0, 0, 0)

    sorted_samples = sorted(samples)
    n = len(sorted_samples)

    return BenchmarkResult(
        name=name,
        samples=n,
        mean_us=statistics.mean(samples),
        median_us=statistics.median(samples),
        min_us=min(samples),
        max_us=max(samples),
        p99_us=sorted_samples[int(n * 0.99)] if n >= 100 else sorted_samples[-1],
        stddev_us=statistics.stdev(samples) if n > 1 else 0,
    )


class EvdevBenchmark:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.source_device: evdev.UInput | None = None
        self.sink_device: evdev.UInput | None = None
        self.reader: evdev.InputDevice[Any] | None = None
        self.reader_path: str | None = None

    def _require_reader(self) -> evdev.InputDevice[Any]:
        if self.reader is None:
            raise RuntimeError("Reader device is not initialized")
        return self.reader

    def _require_source_device(self) -> evdev.UInput:
        if self.source_device is None:
            raise RuntimeError("Source uinput device is not initialized")
        return self.source_device

    def _require_sink_device(self) -> evdev.UInput:
        if self.sink_device is None:
            raise RuntimeError("Sink uinput device is not initialized")
        return self.sink_device

    def setup(self) -> None:
        caps: dict[int, Sequence[int]] = {
            evdev.ecodes.EV_KEY: [
                evdev.ecodes.KEY_A,
                evdev.ecodes.KEY_B,
                evdev.ecodes.KEY_C,
                evdev.ecodes.KEY_SPACE,
                evdev.ecodes.BTN_LEFT,
                evdev.ecodes.BTN_RIGHT,
            ],
            evdev.ecodes.EV_REL: [
                evdev.ecodes.REL_X,
                evdev.ecodes.REL_Y,
                evdev.ecodes.REL_WHEEL,
            ],
        }

        self.source_device = evdev.UInput(events=caps, name="benchmark-source")
        self.sink_device = evdev.UInput(events=caps, name="benchmark-sink")

        for path in evdev.list_devices():
            dev = evdev.InputDevice(path)
            if dev.name == "benchmark-source":
                self.reader = dev
                self.reader_path = path
                break

        if not self.reader:
            raise RuntimeError("Could not find source device")

        self.reader.grab()

        if self.verbose:
            print(f"Source device: {self.reader_path}")
            print(f"Source UInput: {self.source_device.device}")
            print(f"Sink UInput: {self.sink_device.device}")

    def teardown(self) -> None:
        if self.reader:
            try:
                self.reader.ungrab()
            except Exception:
                pass
            self.reader.close()

        if self.source_device:
            self.source_device.close()

        if self.sink_device:
            self.sink_device.close()

    def inject_event(self, event_type: int, code: int, value: int) -> None:
        source_device = self._require_source_device()
        source_device.write(event_type, code, value)
        source_device.syn()

    def read_and_passthrough(self) -> float:
        start = time.perf_counter_ns()

        reader = self._require_reader()
        sink_device = self._require_sink_device()
        events = list(reader.read())
        for event in events:
            if event.type != evdev.ecodes.EV_SYN:
                sink_device.write(event.type, event.code, event.value)
        sink_device.syn()

        end = time.perf_counter_ns()
        return (end - start) / 1000.0

    def benchmark_single_events(self, count: int = 1000) -> BenchmarkResult:
        samples: list[float] = []

        for i in range(count):
            key = evdev.ecodes.KEY_A if i % 2 == 0 else evdev.ecodes.KEY_B

            self.inject_event(evdev.ecodes.EV_KEY, key, 1)
            latency = self.read_and_passthrough()
            samples.append(latency)

            self.inject_event(evdev.ecodes.EV_KEY, key, 0)
            self.read_and_passthrough()

        return calc_stats("Single key events (KEY_A/B press)", samples)

    def benchmark_burst_events(
        self, bursts: int = 100, events_per_burst: int = 10
    ) -> BenchmarkResult:
        samples: list[float] = []
        reader = self._require_reader()
        sink_device = self._require_sink_device()

        for _ in range(bursts):
            start = time.perf_counter_ns()

            for _ in range(events_per_burst):
                self.inject_event(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)
                self.inject_event(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 0)

            for event in reader.read():
                if event.type != evdev.ecodes.EV_SYN:
                    sink_device.write(event.type, event.code, event.value)
            sink_device.syn()

            end = time.perf_counter_ns()
            samples.append((end - start) / 1000.0 / events_per_burst)

        return calc_stats(f"Burst events ({events_per_burst} events/burst)", samples)

    def benchmark_mouse_movement(self, count: int = 1000) -> BenchmarkResult:
        samples: list[float] = []

        for _i in range(count):
            self.inject_event(evdev.ecodes.EV_REL, evdev.ecodes.REL_X, 1)
            self.inject_event(evdev.ecodes.EV_REL, evdev.ecodes.REL_Y, 1)

            latency = self.read_and_passthrough()
            samples.append(latency)

        return calc_stats("Mouse movement (REL_X + REL_Y)", samples)

    def benchmark_uinput_write_only(self, count: int = 1000) -> BenchmarkResult:
        samples: list[float] = []
        sink_device = self._require_sink_device()

        for _ in range(count):
            start = time.perf_counter_ns()
            sink_device.write(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)
            sink_device.syn()
            end = time.perf_counter_ns()
            samples.append((end - start) / 1000.0)

        return calc_stats("UInput write only (no read)", samples)

    def benchmark_evdev_read_only(self, count: int = 1000) -> BenchmarkResult:
        samples: list[float] = []
        reader = self._require_reader()

        for i in range(count):
            key = evdev.ecodes.KEY_A if i % 2 == 0 else evdev.ecodes.KEY_B

            self.inject_event(evdev.ecodes.EV_KEY, key, 1)

            start = time.perf_counter_ns()
            _ = list(reader.read())
            end = time.perf_counter_ns()
            samples.append((end - start) / 1000.0)

        return calc_stats("Evdev read only (no write)", samples)

    def benchmark_full_path(self, count: int = 1000) -> BenchmarkResult:
        samples: list[float] = []
        reader = self._require_reader()
        sink_device = self._require_sink_device()

        for i in range(count):
            key = evdev.ecodes.KEY_A if i % 2 == 0 else evdev.ecodes.KEY_B

            self.inject_event(evdev.ecodes.EV_KEY, key, 1)

            start = time.perf_counter_ns()
            for event in reader.read():
                if event.type != evdev.ecodes.EV_SYN:
                    sink_device.write(event.type, event.code, event.value)
            sink_device.syn()
            end = time.perf_counter_ns()
            samples.append((end - start) / 1000.0)

            self.inject_event(evdev.ecodes.EV_KEY, key, 0)
            for _ in reader.read():
                pass

        return calc_stats("Full path (read + process + write)", samples)

    def benchmark_dict_lookup(self, count: int = 10000) -> BenchmarkResult:
        button_map = {
            "btn_left": "btn_left",
            "btn_right": "btn_right",
            "extra_1": "key_a",
            "extra_2": "key_b",
        }
        evdev_to_button = {v.lower(): k for k, v in button_map.items()}

        samples: list[float] = []
        for _ in range(count):
            start = time.perf_counter_ns()
            evdev_to_button.get("key_a")
            end = time.perf_counter_ns()
            samples.append((end - start) / 1000.0)

        return calc_stats("Dict lookup (button mapping)", samples)

    def benchmark_get_event_name(self, count: int = 1000) -> BenchmarkResult:
        samples: list[float] = []

        for _i in range(count):
            event_type = evdev.ecodes.EV_KEY
            event_code = evdev.ecodes.KEY_A

            start = time.perf_counter_ns()
            try:
                code_name = evdev.ecodes.bytype[event_type].get(event_code, str(event_code))
                if isinstance(code_name, tuple):
                    code_name = code_name[0] if code_name else str(event_code)
                code_name.lower()
            except Exception:
                str(event_code)
            end = time.perf_counter_ns()
            samples.append((end - start) / 1000.0)

        return calc_stats("Event name lookup (ecodes.bytype)", samples)

    def benchmark_with_mapping_check(self, count: int = 1000) -> BenchmarkResult:
        button_map = {
            "btn_left": "btn_left",
            "btn_right": "btn_right",
            "extra_1": "key_a",
            "extra_2": "key_b",
        }
        evdev_to_button = {v.lower(): k for k, v in button_map.items()}
        mapping = {
            "extra_1": {"action": "keyboard", "target": "key_space"},
        }

        samples: list[float] = []
        reader = self._require_reader()
        sink_device = self._require_sink_device()

        for _i in range(count):
            self.inject_event(evdev.ecodes.EV_KEY, evdev.ecodes.KEY_A, 1)

            start = time.perf_counter_ns()

            for event in reader.read():
                if event.type == evdev.ecodes.EV_SYN:
                    continue

                try:
                    code_name = evdev.ecodes.bytype[event.type].get(event.code, str(event.code))
                    if isinstance(code_name, tuple):
                        code_name = code_name[0] if code_name else str(event.code)
                    event_name = code_name.lower()
                except Exception:
                    event_name = str(event.code)

                button_id = evdev_to_button.get(event_name)
                action = mapping.get(button_id) if button_id else None

                if action:
                    pass
                else:
                    sink_device.write(event.type, event.code, event.value)

            sink_device.syn()
            end = time.perf_counter_ns()
            samples.append((end - start) / 1000.0)

        return calc_stats("Full path with mapping check", samples)

    def benchmark_async_loop(self, count: int = 500) -> BenchmarkResult:
        samples: list[float] = []
        reader = self._require_reader()
        sink_device = self._require_sink_device()

        async def run_benchmark():
            loop = asyncio.get_event_loop()

            for i in range(count):
                key = evdev.ecodes.KEY_A if i % 2 == 0 else evdev.ecodes.KEY_B
                self.inject_event(evdev.ecodes.EV_KEY, key, 1)

                start = time.perf_counter_ns()

                events = await loop.run_in_executor(None, list, reader.read())
                for event in events:
                    if event.type != evdev.ecodes.EV_SYN:
                        sink_device.write(event.type, event.code, event.value)
                sink_device.syn()

                end = time.perf_counter_ns()
                samples.append((end - start) / 1000.0)

                self.inject_event(evdev.ecodes.EV_KEY, key, 0)
                await loop.run_in_executor(None, list, reader.read())

        asyncio.run(run_benchmark())
        return calc_stats("Async path (run_in_executor)", samples)


def run_all_benchmarks(verbose: bool = False) -> list[BenchmarkResult]:
    bench = EvdevBenchmark(verbose=verbose)
    bench.setup()

    try:
        results = []

        print("Running benchmarks...")

        print("  1/10 Dict lookup...")
        results.append(bench.benchmark_dict_lookup())

        print("  2/10 Event name lookup...")
        results.append(bench.benchmark_get_event_name())

        print("  3/10 UInput write only...")
        results.append(bench.benchmark_uinput_write_only())

        print("  4/10 Evdev read only...")
        results.append(bench.benchmark_evdev_read_only())

        print("  5/10 Single key events...")
        results.append(bench.benchmark_single_events())

        print("  6/10 Burst events...")
        results.append(bench.benchmark_burst_events())

        print("  7/10 Mouse movement...")
        results.append(bench.benchmark_mouse_movement())

        print("  8/10 Full path...")
        results.append(bench.benchmark_full_path())

        print("  9/10 Full path with mapping check...")
        results.append(bench.benchmark_with_mapping_check())

        print("  10/10 Async path...")
        results.append(bench.benchmark_async_loop())

        return results

    finally:
        bench.teardown()


def print_summary(results: list[BenchmarkResult]) -> None:
    print("\n" + "=" * 60)
    print("BENCHMARK RESULTS")
    print("=" * 60 + "\n")

    for result in results:
        print(result)
        print()

    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    full_path = next(
        (r for r in results if "Full path" in r.name and "mapping" not in r.name.lower()), None
    )
    mapping_path = next((r for r in results if "mapping check" in r.name.lower()), None)

    if full_path:
        print(f"\nBaseline passthrough latency: {full_path.median_us:.2f} us")
        print(f"  At 1000 Hz polling: {full_path.median_us / 1000:.4f} ms (frame budget is 1.0 ms)")
        print(f"  Latency as % of 1ms frame: {full_path.median_us / 10:.2f}%")

    if mapping_path:
        print(f"\nWith mapping overhead: {mapping_path.median_us:.2f} us")
        if full_path:
            overhead = mapping_path.median_us - full_path.median_us
            print(f"  Additional overhead: {overhead:.2f} us")


def main() -> None:
    if os.geteuid() != 0:
        print("Error: This benchmark requires root (sudo) to create uinput devices")
        print("Usage: sudo python benchmarks/evdev_latency.py")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Benchmark evdev passthrough latency")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--samples", type=int, default=1000, help="Number of samples per benchmark")
    args = parser.parse_args()

    results = run_all_benchmarks(verbose=args.verbose)
    print_summary(results)

    print("\n" + "=" * 60)
    print("INTERPRETATION")
    print("=" * 60)
    print("""
Typical human perception thresholds:
  - 1 ms (1000 us): Generally imperceptible for most users
  - 10 ms (10000 us): Noticeable lag in fast-paced games
  - 16.67 ms (16667 us): One frame at 60 Hz
  - 100 ms: Clearly noticeable delay

Typical Python evdev overhead:
  - Dict lookup: <1 us (negligible)
  - Event name resolution: 1-5 us
  - UInput write: 5-20 us
  - Full path: 10-50 us (depends on system)

If your median latency is under 100 us (0.1 ms), Python evdev
should not introduce noticeable lag for most use cases.
""")


if __name__ == "__main__":
    main()
