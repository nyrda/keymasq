MIN_VIRTUAL_GAMEPADS = 0
MAX_VIRTUAL_GAMEPADS = 4
DEFAULT_VIRTUAL_GAMEPADS = 1


def clamp_virtual_gamepad_count(value: object) -> int:
    try:
        count = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        count = DEFAULT_VIRTUAL_GAMEPADS
    return max(MIN_VIRTUAL_GAMEPADS, min(MAX_VIRTUAL_GAMEPADS, count))


def virtual_gamepad_output_id(index: int) -> str:
    if not 1 <= index <= MAX_VIRTUAL_GAMEPADS:
        raise ValueError(f"virtual gamepad index must be 1..{MAX_VIRTUAL_GAMEPADS}")
    return f"virtual-gamepad-{index}"


def is_virtual_gamepad_output_id(output_id: str) -> bool:
    if not output_id.startswith("virtual-gamepad-"):
        return False
    try:
        index = int(output_id.removeprefix("virtual-gamepad-"))
    except ValueError:
        return False
    return 1 <= index <= MAX_VIRTUAL_GAMEPADS and output_id == virtual_gamepad_output_id(index)
