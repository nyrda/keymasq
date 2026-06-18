# pyright: reportUnusedFunction=false

_COMBO_TAB_ID = "combos"
_DEVICE_TAB_PREFIX = "device:"
_DEVICE_TAB_STATE_ICONS = {
    "grabbed": "🟢",
    "partial": "🟡",
    "waiting": "🟡",
    "connected": "🟡",
    "inspector": "🟡",
    "not_connected": "🔴",
    "unknown": "⚪",
}


def _device_tab_id(hardware_id: str) -> str:
    return f"{_DEVICE_TAB_PREFIX}{hardware_id}"
