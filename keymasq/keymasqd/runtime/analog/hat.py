"""Convert a shaped hat position to a stable three-state output."""


def quantize_hat(value: float, previous: int) -> int:
    if value >= 0.55:
        return 1
    if value <= -0.55:
        return -1
    if previous == 1 and value > 0.45:
        return 1
    if previous == -1 and value < -0.45:
        return -1
    return 0
