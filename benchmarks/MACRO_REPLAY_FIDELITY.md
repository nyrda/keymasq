# Macro Replay Fidelity

## Goal

Evaluate whether Keymasq's current Python macro playback path is accurate enough
for dense keyboard/mouse replay, and whether further complexity such as a second
macro engine or native playback worker is justified.

The benchmark is exploratory. It is not part of the unit/integration test suite.

Primary questions:

- How faithfully does the current macro engine replay a known event timeline?
- Did anchored-deadline scheduling materially improve replay fidelity?
- Does `uvloop` provide a meaningful improvement over the default asyncio loop?
- Does lowering Linux timer slack meaningfully improve jitter?

## Benchmark Harness

Implementation: [macro_replay_fidelity.py](macro_replay_fidelity.py)

The benchmark:

1. Creates real `uinput` keyboard and mouse devices.
2. Plays a synthetic macro through the real `DeviceManager.play_macro()` path.
3. Records the emitted output back through `RecordingManager`.
4. Compares expected vs observed event count, ordering, total duration, and
   inter-event gap error.

This measures the full replay path, not just a microbenchmark of one function.

Scenarios used:

- `mouse_click_500cps`
- `mouse_click_1000cps`
- `mouse_click_2000cps`
- `keyboard_667hz_burst`
- `mixed_dense_sequence`

Key metrics:

- `duration_error_us`: total replay duration error
- `abs_gap_mean_us`: mean absolute error of inter-event gaps
- `abs_gap_median_us`: median absolute gap error
- `abs_gap_p95_us`: p95 absolute gap error inside the replay
- `abs_gap_p99_us`: p99 absolute gap error inside the replay

## Commands

Baseline:

```bash
nix develop -c python benchmarks/macro_replay_fidelity.py
```

Use `uvloop`:

```bash
nix develop -c python benchmarks/macro_replay_fidelity.py --uvloop
```

Set timer slack for the benchmark process only:

```bash
nix develop -c python benchmarks/macro_replay_fidelity.py --timerslack-ns 1
```

## Main Finding: Anchored Deadlines Were The Big Win

Before anchored-deadline scheduling, the benchmark showed large accumulated
drift:

- `mouse_click_500cps`: about `+50 ms` total duration error
- `mouse_click_1000cps`: about `+199 ms`
- `mouse_click_2000cps`: event loss / severe replay collapse in the benchmark
- `keyboard_667hz_burst`: about `+68 ms`
- `mixed_dense_sequence`: about `-142 ms`

After switching the playback loop to anchored deadlines, total duration error
became very small:

- `mouse_click_500cps`: about `-0.2 ms`
- `mouse_click_1000cps`: about `-0.4 ms`
- `mouse_click_2000cps`: about `+0.4 ms`
- `keyboard_667hz_burst`: about `+0.3 ms`
- `mixed_dense_sequence`: about `+0.1 ms`

Conclusion:

- The previous fidelity problem was primarily accumulated drift from
  relative-delay scheduling.
- Anchored-deadline scheduling fixed the largest structural accuracy problem.
- This result significantly weakens the case for a second macro subsystem or a
  dedicated native worker, unless lower local jitter is a hard product
  requirement.

## uvloop Comparison

### Method

After the anchored-deadline change, the same benchmark scenarios were run 10
times each under:

- default asyncio loop
- `uvloop`

The goal was to determine whether the apparent `uvloop` improvement from a
single run was stable or just noise.

### Repeated-Run Summary

The numbers below are medians across 10 full benchmark runs.

| Scenario | Default abs gap mean | uvloop abs gap mean | Default abs gap median | uvloop abs gap median |
|---|---:|---:|---:|---:|
| `mouse_click_500cps` | 617 us | 249 us | 248 us | 142 us |
| `mouse_click_1000cps` | 571 us | 558 us | 491 us | 491 us |
| `mouse_click_2000cps` | 384 us | 380 us | 242 us | 241 us |
| `keyboard_667hz_burst` | 625 us | 527 us | 510 us | 428 us |
| `mixed_dense_sequence` | 497 us | 470 us | 382 us | 381 us |

Representative p95 / p99 medians across runs:

| Scenario | Default p95 | uvloop p95 | Default p99 | uvloop p99 |
|---|---:|---:|---:|---:|
| `mouse_click_500cps` | 1276 us | 987 us | 1314 us | 988 us |
| `mouse_click_1000cps` | 723 us | 680 us | 754 us | 710 us |
| `mouse_click_2000cps` | 974 us | 937 us | 1015 us | 975 us |
| `keyboard_667hz_burst` | 736 us | 731 us | 1534 us | 734 us |
| `mixed_dense_sequence` | 918 us | 850 us | 960 us | 902 us |

Duration accuracy was already very good after anchored deadlines under both
loops, so `uvloop` mainly affects local jitter, not overall duration fidelity.

### Interpretation

`uvloop` produced a real and repeatable improvement, but the size of the gain
depends on workload:

- Strong win:
  - `mouse_click_500cps`
- Moderate win:
  - `keyboard_667hz_burst`
  - `mixed_dense_sequence`
- Small win:
  - `mouse_click_1000cps`
  - `mouse_click_2000cps`

Important nuance:

- `uvloop` improves typical gap error more than it improves worst-case behavior.
- It does not move the Python implementation into a new precision class.
- It is an incremental optimization, not a replacement for the anchored-deadline
  fix.

## Timer Slack

Benchmark-only support was added for setting Linux timer slack using
`PR_SET_TIMERSLACK`.

Observed result from single-run spot checks:

- Lowering timer slack alone produced only minor changes.
- `uvloop + low timer slack` was usually a little better than `uvloop` alone.
- The timer slack effect was much smaller than the effect of switching from the
  default loop to `uvloop`.

Conclusion:

- Timer slack is a secondary tuning knob.
- It is not currently justified as a product/runtime change based on the data so
  far.
- If revisited, it should be measured with repeated runs, just like the
  `uvloop` comparison.

## Conclusions

1. Anchored-deadline scheduling fixed the dominant replay-fidelity problem.

2. Python is much more viable for macro playback than the pre-fix benchmark
   suggested.

3. `uvloop` provides a meaningful incremental improvement, especially around the
   `500cps` mouse case and for keyboard burst replay, but it does not
   fundamentally change the architecture discussion.

4. Lower timer slack may help a little, but its effect is smaller and currently
   less convincing than the `uvloop` effect.

5. The benchmark results do not currently justify adding a second macro engine
   or a dedicated native playback worker solely for drift/fidelity reasons.

6. A new subsystem would only become easier to justify if product requirements
   tighten around lower local jitter or stricter sub-millisecond determinism,
   not because the current Python path is obviously broken.

## Current Recommendation

- Keep the single Python macro engine.
- Keep anchored-deadline scheduling.
- Consider `uvloop` as an optional optimization path, not yet a default.
- Treat timer slack as an experimental tuning option, not a runtime policy.
- Revisit a dedicated playback worker only if new benchmark data shows a clear
  remaining product gap that Python cannot reasonably close.
