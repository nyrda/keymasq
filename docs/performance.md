# Performance

## Input latency

Keymasq handles each grabbed input event in its Python daemon before writing
the result to a virtual device. On one test machine (Python 3.12, `powersave`
CPU governor, under load), the daemon's own processing took:

| Input frame | Median | p99 | p99.9 |
|---|---|---|---|
| Mouse movement (`REL_X`, `REL_Y`, `SYN_REPORT`) | 60–63 us | 191–198 us | 296–383 us |
| Key press or release, unmapped or remapped | 68–71 us | 192–195 us | 280–283 us |

That is about 20–40 us of processing per event. The evdev read and uinput
write system calls come on top of this. These figures are from
[benchmarks/pipeline_latency.py](https://github.com/nyrda/keymasq/blob/master/benchmarks/pipeline_latency.py)
and vary with CPU speed and power settings.

### Context

| Reference point | Latency |
|---|---|
| Keymasq processing, one mouse report | 0.06 ms median, under 0.4 ms p99.9 |
| USB polling interval (1000 Hz) | 1 ms |
| USB polling interval (8000 Hz) | 0.125 ms |
| One frame at 144 Hz | 6.9 ms |
| One frame at 60 Hz | 16.7 ms |
| Typical display latency | 5–20 ms |

A 1000 Hz mouse sends a report every millisecond, more than ten times
Keymasq's typical processing time per report.

### Avoiding stalls

Occasional delays matter more than average processing time. keymasqd keeps
other work from holding up grabbed input:

- Profile and combo changes are parsed in slices of about 0.5 ms, and input
  keeps flowing between slices. Input handled during a change uses the
  previous mappings until the new ones are installed together.
- A worker thread running Python hands the event loop back within about 0.2 ms.
- The daemon's startup code is excluded from Python's full garbage
  collections.
- Unsaved recordings play back in small batches, like saved macros, instead of
  being loaded and freed all at once.

Use the `input_latency` diagnostics label to see these delays on your system.

## Macro replay fidelity

Macro playback uses anchored-deadline scheduling to maintain timing accuracy.
Benchmark results show:

- Total duration error: typically under 0.5 ms for dense macro sequences
- Inter-event gap error (median): 140–500 us depending on event density
- Inter-event gap error (p99): under 1 ms in most scenarios

With `uvloop` installed (recommended), jitter improves further, especially for
high-frequency mouse macros and keyboard bursts.

## Live diagnostics

You can measure latency on your own system with the built-in diagnostics mode:

```bash
keymasq diagnostics on --interval 5
journalctl -u keymasqd -f
```

This logs periodic latency percentiles (p50, p95, p99, max) for internal event
processing. By default, diagnostics logs the main input paths most useful for
everyday latency checks. You can add narrower categories when debugging combo
behavior or daemon internals:

```bash
keymasq diagnostics on --include combo
keymasq diagnostics on --include macro
keymasq diagnostics on --include internal
keymasq diagnostics on --include all
keymasq diagnostics on --include all --exclude internal
```

The GTK app includes the same live view under **Diagnostics** in the main menu.
It shows the latest snapshot from keymasqd without reading journal logs.

Disable when done:

```bash
keymasq diagnostics off
```

## Rapidfire throughput

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

### Informal result

On one local test system, six concurrent rapidfire mouse-button mappings
reached roughly **2.8k mouse events/sec** on the `keymasq-mouse` virtual
device while using about **11% of one CPU core**.

That is an informal measurement, not a guaranteed minimum or maximum across
systems. It is a data point that shows very fast rapidfire settings do not
immediately hit an obvious Python-side throughput limit.

### Caveat

Multiple rapidfire mappings targeting the same logical output, such as several
`BTN_LEFT` mappings, do not necessarily scale linearly. Those mappings all
share one output code, so their press and release streams can overlap. Mixed
outputs such as `BTN_LEFT` and `BTN_RIGHT` can scale more cleanly because they
do not collapse onto the same logical button state.

## Detailed benchmarks

For methodology and full results, see:

- [benchmarks/PIPELINE_LATENCY.md](https://github.com/nyrda/keymasq/blob/master/benchmarks/PIPELINE_LATENCY.md) —
  daemon pipeline cost and event-loop stalls
- [benchmarks/LATENCY_ANALYSIS.md](https://github.com/nyrda/keymasq/blob/master/benchmarks/LATENCY_ANALYSIS.md) —
  evdev read/write syscall overhead
- [benchmarks/MACRO_REPLAY_FIDELITY.md](https://github.com/nyrda/keymasq/blob/master/benchmarks/MACRO_REPLAY_FIDELITY.md) —
  macro timing accuracy across scenarios

Keymasq only grabs devices that have active remappings. It does not touch
devices without remappings, and they operate at their native polling rate.

## Diagnostics labels

Diagnostics measure Keymasq's daemon-side handling time for events that pass
through grabbed devices. They are not the full time from your finger movement to
the game or desktop reacting. They do not include USB polling, compositor/game
processing, rendering, display latency, or the physical switch/button travel.
For passthrough devices, normal input events and their source `SYN_REPORT`
frame flush are measured separately so multi-event device frames stay intact.

Most labels start timing when Keymasq begins handling an event. `input_latency`
starts at the kernel's timestamp for the input frame instead, so it also
includes time the frame waited while keymasqd was busy with other work, such
as applying a profile change.

### Mainline diagnostics

Diagnostics shows these by default. They are the most useful labels for
checking normal input overhead.

| Label | What it means | Example |
|---|---|---|
| `input_latency` | Time from the kernel timestamping an input frame to Keymasq finishing that frame's `SYN_REPORT`, including any wait before handling started. | Spikes here with low per-event labels mean the frame was delayed, not slow to process. |
| `passthrough_fast` | An event passed through when the grabbed device has no active mapping work for it. | Moving an unmapped mouse, or clicking an unmapped button on a grabbed device. |
| `passthrough_mapped` | An event passed through while a mapping profile is loaded, but this specific input has no remap action. | Moving the mouse while only one side button is remapped. |
| `passthrough_other` | A non-key, non-relative event passed through. | An uncommon device event such as an absolute axis update. |
| `passthrough_syn` | A pending passthrough frame was flushed at the source device's `SYN_REPORT`. | The frame boundary after grouped touchpad, mouse, or gamepad events. |
| `wheel_passthrough` | A mouse wheel event passed through by an explicit passthrough mapping. | Scrolling normally when the wheel has a passthrough action. |
| `action_*` | A configured remap action ran. The suffix names the action type. | Pressing a remapped button, a suppressed key, or a mapped wheel direction. |

### Combo diagnostics

Enable with:

```bash
keymasq diagnostics on --include combo
```

These labels are useful when tuning combo behavior, but they are not a general
latency baseline.

| Label | What it means | Example |
|---|---|---|
| `combo_passthrough` | A combo-relevant event was checked by the combo engine and then passed through normally. | Pressing the first key of a two-key combo where the key should still type if the combo is not completed. |
| `combo_passthrough_held` | A later event for a combo key that was already allowed through. | Releasing that first combo key after it had been passed through. |
| `combo_release_action_*` | A combo-related release event triggered an action. | Releasing a held combo chord that fires a remapped button. |

### Macro diagnostics

Enable with:

```bash
keymasq diagnostics on --include macro
```

These labels separate stored-macro loading from the complete runtime of one
playback iteration.

| Label | What it means |
|---|---|
| `macro_load` | Time spent resolving the stored revision and obtaining its events. Streamed iterations include worker batches that decompress and parse the file, while cache hits include only revision validation and cache retrieval. Timeline sleeps, wait controls, and command execution are excluded. |
| `macro_iteration` | Wall-clock runtime of one normally completed iteration, including event timing, explicit and random waits, synchronous commands, and trailing macro duration. Inter-iteration scheduling yields are excluded. |

Only stored macros are sampled. Cancelled, interrupted, or failed iterations
do not produce a complete iteration sample.

### Internal diagnostics

Enable with:

```bash
keymasq diagnostics on --include internal
```

These labels are mostly for development and bug reports.

| Label | What it means | Example |
|---|---|---|
| `syn` | A Linux synchronization event was received without a pending passthrough frame to flush. | A separator event that did not emit output. |
| `wheel_high_res_suppressed` | A high-resolution wheel event was suppressed because the matching low-resolution wheel event is being handled. | A mouse wheel that reports both high-res and normal scroll events. |
| `combo_recalled_repeat_suppressed` | A repeat event was suppressed for a combo trigger key that Keymasq temporarily recalled. | Holding a key involved in combo recall long enough for keyboard repeat to start. |
| `combo_recalled_release_suppressed` | A release event was suppressed after combo recall cleanup. | Releasing a combo trigger key that Keymasq already restored synthetically. |
