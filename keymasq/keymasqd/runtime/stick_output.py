"""Keep gyro offsets separate from the most recent ordinary stick write."""

from dataclasses import dataclass, field


@dataclass
class _GyroAxis:
    hardware_id: str
    owner: tuple[int, str]
    offset: float
    minimum: int
    maximum: int
    center: int
    minimum_output: float

    def apply(self, base: int) -> int:
        value = max(self.minimum, min(self.maximum, base + self.offset))
        displacement = value - self.center
        if self.offset != 0.0 and displacement != 0.0:
            endpoint = self.maximum if displacement > 0 else self.minimum
            value = (
                self.center
                + (endpoint - self.center) * self.minimum_output
                + displacement * (1.0 - self.minimum_output)
            )
        return int(round(value))


@dataclass
class StickOutputState:
    bases: dict[int, tuple[str, int]] = field(default_factory=dict)
    gyros: dict[int, _GyroAxis] = field(default_factory=dict)

    def write_base(self, hardware_id: str, code: int, value: int) -> int:
        self.bases[code] = (hardware_id, value)
        gyro = self.gyros.get(code)
        if gyro is not None and gyro.hardware_id == hardware_id:
            return gyro.apply(value)
        return value

    def write_gyro(
        self,
        hardware_id: str,
        owner: tuple[int, str],
        code: int,
        minimum: int,
        maximum: int,
        center: int,
        normalized: float,
        minimum_output: float = 0.0,
    ) -> int:
        span = maximum - center if normalized >= 0.0 else center - minimum
        gyro = _GyroAxis(
            hardware_id, owner, normalized * span, minimum, maximum, center, minimum_output
        )
        self.gyros[code] = gyro
        return gyro.apply(self._paired_base(code, gyro))

    def release_gyro(self, owner: tuple[int, str], code: int) -> int | None:
        gyro = self.gyros.get(code)
        if gyro is None or gyro.owner != owner:
            return None
        del self.gyros[code]
        return self._paired_base(code, gyro)

    def _paired_base(self, code: int, gyro: _GyroAxis) -> int:
        hardware_id, value = self.bases.get(code, (gyro.hardware_id, gyro.center))
        return value if hardware_id == gyro.hardware_id else gyro.center
