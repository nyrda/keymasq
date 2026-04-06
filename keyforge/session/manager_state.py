from dataclasses import dataclass, field

from keyforge.session.manager_common import JsonObject


def default_recording_settings() -> JsonObject:
    return {
        "include_mouse_movement": False,
        "include_mouse_clicks": False,
        "record_start_position": False,
        "record_keyboard": True,
        "record_mouse": False,
        "record_gamepad": True,
        "device_overrides": {},
    }


@dataclass
class CaptureRuntimeState:
    locks: set[str] = field(default_factory=set)
    resume_profiles: dict[str, list[str]] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)


@dataclass
class RecordingRuntimeState:
    active: bool = False
    pending_data: JsonObject | None = None
    start_cursor: tuple[int, int] | None = None
    settings: JsonObject = field(default_factory=default_recording_settings)
    settings_pending_save: JsonObject | None = None
    settings_save_task: object | None = None
    devices_cache: list[JsonObject] = field(default_factory=list)


@dataclass
class UnlockRuntimeState:
    refresh_owner: JsonObject | None = None
    runtime_refresh_claim_consumed_until: dict[int, int] = field(default_factory=dict)
    refresh_ttl_s: int = 60
