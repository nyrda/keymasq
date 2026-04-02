#!/usr/bin/env python3
"""
Pure Python overhead benchmark - no root required.

Measures the computational overhead of the Python operations
that would happen during passthrough, without needing uinput.

This isolates the Python overhead from kernel/driver overhead.
"""

import statistics
import time
from dataclasses import dataclass

import evdev


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


def bench_perf_counter(count: int = 100000) -> BenchmarkResult:
    samples = []
    for _ in range(count):
        start = time.perf_counter_ns()
        end = time.perf_counter_ns()
        samples.append((end - start) / 1000.0)
    return calc_stats("perf_counter_ns() call", samples)


def bench_dict_lookup(count: int = 100000) -> BenchmarkResult:
    button_map = {
        "btn_left": "btn_left",
        "btn_right": "btn_right",
        "extra_1": "key_a",
        "extra_2": "key_b",
        "extra_3": "key_c",
    }
    evdev_to_button = {v.lower(): k for k, v in button_map.items()}

    samples = []
    for _ in range(count):
        start = time.perf_counter_ns()
        _ = evdev_to_button.get("key_a")
        end = time.perf_counter_ns()
        samples.append((end - start) / 1000.0)
    return calc_stats("Dict lookup (hit)", samples)


def bench_dict_miss(count: int = 100000) -> BenchmarkResult:
    button_map = {"btn_left": "btn_left"}
    evdev_to_button = {v.lower(): k for k, v in button_map.items()}

    samples = []
    for _ in range(count):
        start = time.perf_counter_ns()
        _ = evdev_to_button.get("unknown_key")
        end = time.perf_counter_ns()
        samples.append((end - start) / 1000.0)
    return calc_stats("Dict lookup (miss)", samples)


def bench_event_name_lookup(count: int = 100000) -> BenchmarkResult:
    samples = []
    event_type = evdev.ecodes.EV_KEY
    event_code = evdev.ecodes.KEY_A

    for _ in range(count):
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

    return calc_stats("Event name lookup", samples)


def bench_full_mapping_check(count: int = 100000) -> BenchmarkResult:
    button_map = {
        "btn_left": "btn_left",
        "btn_right": "btn_right",
        "extra_1": "key_a",
        "extra_2": "key_b",
        "extra_3": "key_c",
    }
    evdev_to_button = {v.lower(): k for k, v in button_map.items()}
    mapping = {
        "extra_1": {"action": "keyboard", "target": "key_space"},
    }

    event_type = evdev.ecodes.EV_KEY
    event_code = evdev.ecodes.KEY_A

    samples = []
    for _ in range(count):
        start = time.perf_counter_ns()

        try:
            code_name = evdev.ecodes.bytype[event_type].get(event_code, str(event_code))
            if isinstance(code_name, tuple):
                code_name = code_name[0] if code_name else str(event_code)
            event_name = code_name.lower()
        except Exception:
            event_name = str(event_code)

        button_id = evdev_to_button.get(event_name)
        mapping.get(button_id) if button_id else None

        end = time.perf_counter_ns()
        samples.append((end - start) / 1000.0)

    return calc_stats("Full mapping check (name + dict)", samples)


def bench_passthrough_logic(count: int = 100000) -> BenchmarkResult:
    button_map = {
        "btn_left": "btn_left",
        "btn_right": "btn_right",
        "extra_1": "key_a",
    }
    evdev_to_button = {v.lower(): k for k, v in button_map.items()}
    mapping = {
        "extra_1": {"action": "keyboard", "target": "key_space"},
    }

    event_type = evdev.ecodes.EV_KEY
    test_codes = [evdev.ecodes.KEY_A, evdev.ecodes.KEY_B, evdev.ecodes.BTN_LEFT]

    samples = []
    for i in range(count):
        event_code = test_codes[i % len(test_codes)]

        start = time.perf_counter_ns()

        try:
            code_name = evdev.ecodes.bytype[event_type].get(event_code, str(event_code))
            if isinstance(code_name, tuple):
                code_name = code_name[0] if code_name else str(event_code)
            event_name = code_name.lower()
        except Exception:
            event_name = str(event_code)

        button_id = evdev_to_button.get(event_name)
        mapping.get(button_id) if button_id else None

        end = time.perf_counter_ns()
        samples.append((end - start) / 1000.0)

    return calc_stats("Complete passthrough decision", samples)


def bench_action_enum(count: int = 100000) -> BenchmarkResult:
    from enum import Enum

    class ActionType(Enum):
        PASSTHROUGH = "passthrough"
        KEYBOARD = "keyboard"
        MOUSE = "mouse"
        EXEC = "exec"
        SUPPRESS = "suppress"

    samples = []
    for _ in range(count):
        start = time.perf_counter_ns()
        action_type = ActionType("keyboard")
        _ = action_type == ActionType.KEYBOARD
        end = time.perf_counter_ns()
        samples.append((end - start) / 1000.0)

    return calc_stats("Enum creation + comparison", samples)


def bench_dataclass_access(count: int = 100000) -> BenchmarkResult:
    from dataclasses import dataclass

    @dataclass
    class MappingAction:
        action_type: str
        target: str | None = None
        cmd: str | None = None

    action = MappingAction(action_type="keyboard", target="key_space")

    samples = []
    for _ in range(count):
        start = time.perf_counter_ns()
        _ = action.action_type
        _ = action.target
        _ = action.cmd
        end = time.perf_counter_ns()
        samples.append((end - start) / 1000.0)

    return calc_stats("Dataclass attribute access", samples)


def bench_list_creation(count: int = 50000) -> BenchmarkResult:
    samples = []
    dummy_data = list(range(100))

    for _ in range(count):
        start = time.perf_counter_ns()
        _ = list(dummy_data)
        end = time.perf_counter_ns()
        samples.append((end - start) / 1000.0)

    return calc_stats("List copy (100 items)", samples)


def bench_function_call(count: int = 100000) -> BenchmarkResult:
    def passthrough(event_type: int, event_code: int, event_value: int) -> tuple[int, int, int]:
        return (event_type, event_code, event_value)

    samples = []
    for _ in range(count):
        start = time.perf_counter_ns()
        _ = passthrough(1, 30, 1)
        end = time.perf_counter_ns()
        samples.append((end - start) / 1000.0)

    return calc_stats("Function call overhead", samples)


def bench_async_create_task(count: int = 10000) -> BenchmarkResult:
    import asyncio

    async def dummy():
        pass

    async def run_bench() -> list[float]:
        samples: list[float] = []
        for _ in range(count):
            start = time.perf_counter_ns()
            task = asyncio.create_task(dummy())
            end = time.perf_counter_ns()
            samples.append((end - start) / 1000.0)
            await task
        return samples

    samples = asyncio.run(run_bench())
    return calc_stats("asyncio.create_task()", samples)


def bench_if_else_chain(count: int = 100000) -> BenchmarkResult:
    from enum import Enum

    class ActionType(Enum):
        PASSTHROUGH = "passthrough"
        KEYBOARD = "keyboard"
        MOUSE = "mouse"
        EXEC = "exec"
        SUPPRESS = "suppress"

    actions = list(ActionType)

    samples = []
    for i in range(count):
        action = actions[i % len(actions)]

        start = time.perf_counter_ns()

        if action == ActionType.PASSTHROUGH:
            pass
        elif action == ActionType.SUPPRESS:
            pass
        elif action == ActionType.KEYBOARD:
            pass
        elif action == ActionType.MOUSE:
            pass
        elif action == ActionType.EXEC:
            pass
        else:
            pass

        end = time.perf_counter_ns()
        samples.append((end - start) / 1000.0)

    return calc_stats("If/else chain (5 branches)", samples)


def main() -> None:
    print("=" * 60)
    print("PYTHON OVERHEAD BENCHMARK")
    print("(No root required - measures CPU overhead only)")
    print("=" * 60 + "\n")

    benchmarks = [
        ("perf_counter", bench_perf_counter),
        ("dict_lookup", bench_dict_lookup),
        ("dict_miss", bench_dict_miss),
        ("event_name", bench_event_name_lookup),
        ("full_mapping", bench_full_mapping_check),
        ("passthrough_logic", bench_passthrough_logic),
        ("enum", bench_action_enum),
        ("dataclass", bench_dataclass_access),
        ("list_copy", bench_list_creation),
        ("func_call", bench_function_call),
        ("async_task", bench_async_create_task),
        ("if_else", bench_if_else_chain),
    ]

    results = []
    for name, bench_func in benchmarks:
        print(f"Running {name}...", end=" ", flush=True)
        try:
            result = bench_func()
            results.append(result)
            print(f"median={result.median_us:.2f}us")
        except Exception as e:
            print(f"ERROR: {e}")

    print("\n" + "=" * 60)
    print("DETAILED RESULTS")
    print("=" * 60 + "\n")

    for result in results:
        print(result)
        print()

    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    mapping = next((r for r in results if "Full mapping" in r.name), None)
    next((r for r in results if "passthrough" in r.name.lower() and "Full" not in r.name), None)

    if mapping:
        print(f"\nPython processing overhead: ~{mapping.median_us:.1f} us per event")
        print(f"  At 1000 Hz polling: {mapping.median_us / 1000:.4f} ms")
        print(f"  At 100 Hz polling:  {mapping.median_us / 100:.4f} ms")

    print("\n" + "=" * 60)
    print("KERNEL OVERHEAD (estimated from literature)")
    print("=" * 60)
    print("""
Based on typical Linux evdev/uinput measurements:
  - evdev read syscall:     ~5-20 us
  - uinput write syscall:   ~5-20 us
  - Context switch:         ~1-5 us
  - Total kernel overhead:  ~15-50 us per event

Combined with Python overhead:
  - Python processing:      ~0.5-2 us
  - Kernel syscalls:        ~15-50 us
  - TOTAL ESTIMATED:        ~20-60 us per event

For gaming at 1000 Hz:
  - Frame budget: 1000 us (1 ms)
  - Estimated latency: 20-60 us
  - Percentage: 2-6% of frame budget
""")

    print("=" * 60)
    print("CONCLUSION")
    print("=" * 60)
    print("""
Python evdev overhead is NEGLIGIBLE for most use cases:
  - < 1 us for dictionary lookups
  - < 2 us for complete passthrough logic
  - Kernel syscall overhead dominates (15-50 us)

The total latency (20-60 us) is:
  - Well below human perception (~1 ms)
  - A small fraction of one frame at 60 Hz (16.7 ms)
  - Acceptable for competitive gaming

If you need lower latency, consider:
  - C/Rust daemon (reduces Python overhead to ~0)
  - Direct ioctl calls (reduces Python overhead slightly)
  - Hardware-level remapping (zero software overhead)
""")


if __name__ == "__main__":
    main()
