# Evdev Passthrough Latency Analysis

## Summary

Python evdev overhead is **negligible** for key remapping use cases. The total passthrough latency is dominated by kernel syscalls, not Python processing.

## Benchmark Results

### Python Processing Overhead

| Operation | Median Latency |
|-----------|---------------|
| Dictionary lookup | 0.08 us |
| Event name resolution | 0.24 us |
| Full mapping check | 0.29 us |
| Complete passthrough decision | 0.31 us |
| Dataclass attribute access | 0.08 us |
| Function call | 0.09 us |
| If/else chain (5 branches) | 0.15 us |
| `asyncio.create_task()` | 1.12 us |

**Key finding**: The complete passthrough logic (name lookup + mapping check + decision) takes only ~0.3 microseconds.

### Estimated Total Latency

| Component | Latency |
|-----------|---------|
| Python processing | ~0.5 us |
| evdev read syscall | ~5-20 us |
| uinput write syscall | ~5-20 us |
| Context switch overhead | ~1-5 us |
| **TOTAL ESTIMATED** | **~15-50 us** |

## Comparison to Human Perception

| Threshold | Latency | Notes |
|-----------|---------|-------|
| Our estimated latency | 15-50 us | 0.015-0.05 ms |
| Generally imperceptible | < 1 ms | 1000 us |
| Noticeable in fast games | ~10 ms | 10000 us |
| One frame at 60 Hz | 16.67 ms | 16667 us |
| One frame at 144 Hz | 6.94 ms | 6944 us |
| One frame at 1000 Hz | 1 ms | 1000 us |

**Conclusion**: Our latency (15-50 us) is:
- 20-60x lower than human perception threshold (1 ms)
- 300-1000x lower than one frame at 60 Hz
- Still only 2-5% of frame budget at 1000 Hz

## Real-World Context

### USB Polling Rates
- USB Full Speed: 1000 Hz (1 ms intervals)
- USB High Speed: 8000 Hz (0.125 ms intervals) - rare for input devices

### Hardware Latency Sources
USB devices already have inherent latency:
- USB poll interval: 1-8 ms (125-1000 Hz)
- Controller processing: 1-5 ms
- Display latency: 5-20 ms

The Python evdev overhead (~0.05 ms) is **orders of magnitude smaller** than any of these.

## Recommendations

### No Action Needed
Python evdev is perfectly acceptable for:
- General desktop use
- Casual gaming
- Accessibility applications
- Most competitive gaming scenarios

### Consider Alternatives If:
- You're a professional esports player needing < 1 ms latency
- You need 8000 Hz polling rate support
- Your input device has sub-millisecond latency specs
- You're doing latency-critical research

## Conclusion

**Python evdev is not too slow.** The benchmark shows:

1. Python processing overhead: ~0.3 us per event
2. Total estimated latency: 15-50 us
3. This is 20-60x below human perception
4. Kernel syscall overhead dominates, not Python

The keyforge implementation using Python evdev is well-suited for its purpose. The latency introduced by Python is negligible compared to:
- USB polling intervals (1000 us)
- Display latency (5000-20000 us)
- Human reaction time (100000+ us)

No rewrite in C/Rust is necessary unless you have specific sub-millisecond latency requirements.
