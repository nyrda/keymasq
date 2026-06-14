from __future__ import annotations

NATURAL_MOVE_SPEED_SCALE = 1000.0
NATURAL_MOVE_SPEED_MAX_KPX_S = 100.0


def speed_px_s_to_kpx_s(speed_px_s: float) -> float:
    return float(max(1, round(float(speed_px_s) / NATURAL_MOVE_SPEED_SCALE)))


def speed_kpx_s_to_px_s(speed_kpx_s: float) -> float:
    return float(max(1, round(float(speed_kpx_s)))) * NATURAL_MOVE_SPEED_SCALE


def format_natural_move_speed(speed_px_s: float) -> str:
    return f"{speed_px_s_to_kpx_s(speed_px_s):.0f}kpx/s"
