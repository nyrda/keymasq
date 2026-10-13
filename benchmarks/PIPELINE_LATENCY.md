# Pipeline Latency and Event-Loop Stalls

## Goal

Measure the cost of keymasqd's real grabbed-device pipeline per input frame,
and find work that delays grabbed input for longer than processing it does.

[LATENCY_ANALYSIS.md](LATENCY_ANALYSIS.md) times dictionary lookups and a bare
evdev read/write loop. It does not run Keymasq's pipeline, so its ~0.3 us
"Python processing" figure is much lower than the real cost.

## Harness

Implementation: [pipeline_latency.py](pipeline_latency.py)

```bash
nix develop -c python benchmarks/pipeline_latency.py --uvloop
```

No root or uinput access is needed. The benchmark:

1. Sends synthetic frames through `pipeline.process_event` on a grabbed device
   with a real `DeviceManager` and combo engine, discarding output writes.
2. Applies profile mappings and combos with `set_mapping` and `set_combos`
   while timing the longest stretch without an event-loop iteration.
3. Sends timestamped wakeups through a pipe at 1 kHz and measures how late the
   event loop handles them while an `asyncio.to_thread` worker runs Python.
4. Times a full garbage collection of the daemon's import-time heap with and
   without `gc.freeze()`.

## Results

One run on Python 3.12 with uvloop, `powersave` CPU governor, load average 5:

| Pipeline frame | Median | p99 | p99.9 |
|---|---:|---:|---:|
| Mouse move, device without mappings | 19.9 us | 69.9 us | 128.5 us |
| Mouse move, profile with mappings | 23.0 us | 74.9 us | 137.3 us |
| Unmapped key press or release | 40.7 us | 115.5 us | 171.2 us |
| Remapped key press or release | 51.7 us | 139.4 us | 216.6 us |

Before the per-event work below was removed, the same run on the same machine
measured 60.0, 63.7, 71.5 and 68.6 us medians, with p99 values of 163–196 us.
Most of that time was spent on work that did not depend on the event:

- resolving each event's name from the ecodes tables, now cached per type
  and code
- running the combo callback, recall check and wheel handling for mouse
  movement and sync events, which combos and wheel handling never use
- repeat bookkeeping for mouse movement, and copying a freshly built action
  for every passed-through key press
- converting event fields in every branch, and awaiting the analog observer
  for events it ignores

A deterministic pure-Python workload on the same machine showed the same
spread between median and p99, so the tail here is machine noise rather than
pipeline behavior. No pipeline frame took longer than 1 ms.

| Profile change | Total | Longest loop block |
|---|---:|---:|
| `set_mapping`, 50 keys | 1320 us | 536 us |
| `set_mapping`, 50 keys with 20 superkeys | 2955 us | 576 us |
| `set_combos`, 10 combos | 619 us | 629 us |
| `set_combos`, 40 combos | 2046 us | 990 us |

Before parsing was split into ~0.5 ms slices, a 100-key mapping blocked the
loop for 3.3 ms, or 4.7 ms with 20 superkeys, and 40 combos for 2.1 ms. The
remaining `set_combos` block is the atomic installation of the parsed combos.

| 1 kHz wakeups | Median | p99 | max | Over 1 ms |
|---|---:|---:|---:|---:|
| Idle | 10.9 us | 26.1 us | 72.4 us | 0 |
| Python worker, 5 ms switch interval | 3015 us | 9201 us | 9500 us | 2497 of 3000 |
| Python worker, 0.2 ms switch interval | 272 us | 316 us | 469 us | 0 |

A full collection of the 51,060 objects tracked after importing the daemon took
4.67 ms, and nothing after `gc.freeze()`.

## Conclusions

1. Python processing is about 7–26 us per event, not ~0.3 us. That is far
   below a 1 ms USB polling interval.
2. The large delays came from other work holding up input, not from per-event
   processing: profile changes parsed in one block, worker threads holding
   the GIL for up to the 5 ms default switch interval, and full collections
   scanning the import-time heap.
3. keymasqd now parses profile changes in slices, sets a 0.2 ms switch
   interval, freezes the import-time heap at startup, and streams pending
   recordings during playback.

Freeing many objects at once cannot be interrupted, whichever thread does it:
30,000 decoded macro events took 3.35 ms to free. Pending recordings used to be
decoded into one list that was freed when playback ended. They now play back
in batches of 128 events, as saved macros do.

## Known remaining stalls

- Installing a new combo set blocks for about 1 ms with 40 combos.

The `input_latency` diagnostics label measures from the kernel timestamp of
each input frame, so it shows these delays on real hardware.
