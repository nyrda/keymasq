# Performance

## Input Latency

Keymasq adds **15–50 microseconds** (0.015–0.05 ms) to input events during
remapping. This is the round-trip time through the Python userspace daemon,
including kernel syscalls for reading from evdev and writing to uinput.

| Component | Latency |
|---|---|
| Python processing | ~0.5 us |
| evdev read syscall | ~5–20 us |
| uinput write syscall | ~5–20 us |
| Context switch overhead | ~1–5 us |
| **Total** | **~15–50 us** |

### Context

| Reference Point | Latency |
|---|---|
| Keymasq overhead | 0.015–0.05 ms |
| Human perception threshold | ~1 ms |
| One frame at 144 Hz | 6.9 ms |
| One frame at 60 Hz | 16.7 ms |
| USB polling interval (1000 Hz) | 1 ms |
| Typical display latency | 5–20 ms |

Keymasq's overhead is 20–60x lower than the threshold where latency becomes
perceptible, and 100–300x lower than a single frame at typical refresh rates.
For competitive gaming, the latency is still only 2–5% of the frame budget
even at 1000 Hz.

## Macro Replay Fidelity

Macro playback uses anchored-deadline scheduling to maintain timing accuracy.
Benchmark results show:

- Total duration error: typically under 0.5 ms for dense macro sequences
- Inter-event gap error (median): 140–500 us depending on event density
- Inter-event gap error (p99): under 1 ms in most scenarios

With `uvloop` installed (recommended), jitter improves further, especially for
high-frequency mouse macros and keyboard bursts.

## Live Diagnostics

You can measure latency on your own system with the built-in diagnostics mode:

```bash
keymasq diagnostics on --interval 5
journalctl -u keymasqd -f
```

This logs periodic latency percentiles (p50, p95, p99, max) for internal event
processing. Disable when done:

```bash
keymasq diagnostics off
```

## Rapidfire Throughput

You can measure rapidfire output rate externally on the Keymasq virtual output
device with `evtest` and `pv`.

First, identify the relevant Keymasq virtual device:

```bash
sudo evtest
```

Then count emitted key state changes on that device:

```bash
sudo evtest /dev/input/eventX \
  | rg --line-buffered 'EV_KEY.*value [01]' \
  | pv -l -i 1 > /dev/null
```

This reports total press and release events per second. If you only want
activation rate, count key-down events only:

```bash
sudo evtest /dev/input/eventX \
  | rg --line-buffered 'EV_KEY.*value 1' \
  | pv -l -i 1 > /dev/null
```

### Informal Result

On one local test system, six concurrent rapidfire mouse-button mappings
reached roughly **2.8k mouse events/sec** on the `keymasq-mouse` virtual
device while using about **11% of one CPU core**.

That is an informal measurement, not a guaranteed minimum or maximum across
systems. It is included here as a practical data point showing that very fast
rapidfire settings do not immediately hit an obvious Python-side throughput
limit.

### Caveat

Multiple rapidfire mappings targeting the same logical output, such as several
`BTN_LEFT` mappings, do not necessarily scale linearly. Those mappings all
share one output code, so their press and release streams can overlap. Mixed
outputs such as `BTN_LEFT` and `BTN_RIGHT` can scale more cleanly because they
do not collapse onto the same logical button state.

## Detailed Benchmarks

For methodology and full results, see:

- [benchmarks/LATENCY_ANALYSIS.md](../benchmarks/LATENCY_ANALYSIS.md) — evdev
  passthrough latency breakdown
- [benchmarks/MACRO_REPLAY_FIDELITY.md](../benchmarks/MACRO_REPLAY_FIDELITY.md) —
  macro timing accuracy across scenarios

Keymasq only grabs devices that have active remappings. Devices without
remappings are not touched and operate at their native polling rate.
