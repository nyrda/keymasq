"""Per-device pointer movement runtime state."""

import asyncio
from collections import deque
from dataclasses import dataclass, field

from keymasq.common.model.analog import AnalogControlConfig
from keymasq.common.model.pointer import PointerMovementConfig


@dataclass
class PointerMovementState:
    config: PointerMovementConfig | None = None
    axis_configs: tuple[AnalogControlConfig | None, AnalogControlConfig | None] = (None, None)
    frame_x: float = 0.0
    frame_y: float = 0.0
    frame_pending: bool = False
    remainder_x: float = 0.0
    remainder_y: float = 0.0
    samples: deque[tuple[int, float, float]] = field(default_factory=deque)
    position_x: float = 0.0
    position_y: float = 0.0
    last_motion_ns: int = 0
    last_arrival_ns: int = 0
    task: asyncio.Task[None] | None = None
