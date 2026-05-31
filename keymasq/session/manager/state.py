import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal

from keymasq.common.models import ProfileDeactivationPolicy
from keymasq.session.listeners.base import WindowListener
from keymasq.session.profiles import ResolvedCombo, ResolvedDeviceProfile

from .common import JsonObject


def default_recording_settings() -> JsonObject:
    return {
        "include_mouse_movement": False,
        "include_mouse_clicks": False,
        "record_start_position": False,
        "device_overrides": {},
    }


@dataclass
class CaptureRuntimeState:
    locks: set[str] = field(default_factory=set)
    resume_profiles: dict[str, list[str]] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)
    owner_writer_ids: dict[str, int] = field(default_factory=dict)


@dataclass
class RecordingRuntimeState:
    active: bool = False
    active_slot: int = 0
    pending_data: JsonObject | None = None
    pending_slots: dict[int, JsonObject] = field(default_factory=dict)
    pending_slot_tokens: dict[int, str] = field(default_factory=dict)
    pending_slot_owner_writer_ids: dict[int, int | None] = field(default_factory=dict)
    pending_slot_owner_pids: dict[int, int | None] = field(default_factory=dict)
    pending_slot_owner_uids: dict[int, int | None] = field(default_factory=dict)
    pending_slot_created_at: dict[int, float] = field(default_factory=dict)
    pending_save_token: str | None = None
    pending_save_owner_writer_id: int | None = None
    pending_save_owner_pid: int | None = None
    pending_save_owner_uid: int | None = None
    pending_save_created_at: float = 0.0
    active_owner_writer_id: int | None = None
    active_owner_pid: int | None = None
    active_owner_uid: int | None = None
    start_cursor: tuple[int, int] | None = None
    settings: JsonObject = field(default_factory=default_recording_settings)
    settings_pending_save: JsonObject | None = None
    settings_save_task: object | None = None
    devices_cache: list[JsonObject] = field(default_factory=list)
    selected_devices_cache: list[JsonObject] = field(default_factory=list)
    devices_cache_ready: bool = False


@dataclass
class UnlockRuntimeState:
    refresh_owner: JsonObject | None = None
    runtime_refresh_claim_consumed_until: dict[int, int] = field(default_factory=dict)
    refresh_ttl_s: int = 60


@dataclass
class DeviceInspectorRuntimeState:
    active_hardware_ids: set[str] = field(default_factory=set)
    suppressed_hardware_ids: set[str] = field(default_factory=set)
    owners_by_hardware_id: dict[str, set[int]] = field(default_factory=dict)


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
    resolved_combos: list[ResolvedCombo] = field(default_factory=list)
    runtime_profile_activations: dict[str, "RuntimeProfileActivation"] = field(
        default_factory=dict
    )
    runtime_profile_activation_seq: int = 0
    apply_generation: int = 0
    apply_task: asyncio.Task[None] | None = None
    apply_reason: str = ""


@dataclass
class RuntimeProfileActivation:
    profile_name: str
    activation_id: str
    sequence: int
    deactivation: ProfileDeactivationPolicy
    source_device: str = ""
    source_button: str = ""
    trigger_id: str = ""
    created_at: float = 0.0


@dataclass
class ExecBinding:
    cmd: str
    owner: Literal["device", "combo"]
    hardware_id: str | None = None


@dataclass
class ExecRuntimeState:
    exec_refs: dict[int, ExecBinding] = field(default_factory=dict)
    next_exec_ref: int = 1
    device_exec_refs: dict[str, set[int]] = field(default_factory=dict)
    combo_exec_refs: set[int] = field(default_factory=set)


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
    support_details_cache: JsonObject = field(default_factory=dict)
    support_details_cache_compositor_id: str | None = None
    support_details_cache_at: float = 0.0
    support_details_cache_ttl_s: float = 5.0
    support_details_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class EventRuntimeState:
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
