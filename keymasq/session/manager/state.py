import asyncio
from dataclasses import dataclass, field

from keymasq.session.listeners.base import WindowListener
from keymasq.session.profiles import ResolvedDeviceProfile

from .common import JsonObject


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
    pending_save_token: str | None = None
    pending_save_owner_writer_id: int | None = None
    pending_save_owner_pid: int | None = None
    pending_save_owner_uid: int | None = None
    pending_save_created_at: float = 0.0
    pending_save_notification_token: str | None = None
    pending_save_notification_at: float = 0.0
    active_owner_writer_id: int | None = None
    active_owner_pid: int | None = None
    active_owner_uid: int | None = None
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


@dataclass
class ProfileRuntimeState:
    grabbed_devices: set[str] = field(default_factory=set)
    grabbed_interfaces: dict[str, dict[str, str]] = field(default_factory=dict)
    grab_waiting_devices: set[str] = field(default_factory=set)
    grab_retry_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    topology_refresh_task: asyncio.Task[None] | None = None
    last_sent_grab_signatures: dict[str, str] = field(default_factory=dict)
    last_sent_mapping_signatures: dict[str, str] = field(default_factory=dict)
    last_sent_combo_signature: str = ""
    active_profile_names: list[str] = field(default_factory=list)
    resolved_devices: dict[str, ResolvedDeviceProfile] = field(default_factory=dict)


@dataclass
class ExecRuntimeState:
    exec_refs: dict[int, str] = field(default_factory=dict)
    next_exec_ref: int = 1
    device_exec_refs: dict[str, set[int]] = field(default_factory=dict)
    combo_exec_refs: set[int] = field(default_factory=set)
    combo_superkey_exec_refs: set[int] = field(default_factory=set)
    superkey_exec_refs: dict[int, tuple[str, str]] = field(default_factory=dict)
    next_superkey_exec_ref: int = 10000


@dataclass
class CompositorRuntimeState:
    current_window: JsonObject = field(default_factory=dict)
    window_listener: WindowListener | None = None
    compositor_id: str | None = None
    compositor_capabilities: list[str] = field(default_factory=list)
    supervisor_task: asyncio.Task[None] | None = None
    candidate: str | None = None
    candidate_hits: int = 0
    probe_fast_s: float = 1.0
    probe_slow_s: float = 5.0
    listener_retry_after: dict[str, float] = field(default_factory=dict)
    listener_last_error: dict[str, str] = field(default_factory=dict)
    listener_last_log_at: dict[str, float] = field(default_factory=dict)
    listener_retry_interval_s: float = 30.0
    listener_log_interval_s: float = 60.0
    last_listener_start_error: str = ""
