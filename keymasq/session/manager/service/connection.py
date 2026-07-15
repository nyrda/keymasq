import asyncio
import logging
from typing import Any, cast

from keymasq.common.ipc import Command, CommandType
from keymasq.common.paths import SOCKET_PATH

from .. import recording_device_selection, recording_lifecycle
from ..common import JsonObject
from ..profile import coordinator

log = logging.getLogger("keymasq-session")


class DaemonConnectionMixin:
    async def _sync_virtual_gamepads_to_daemon(self: Any) -> None:
        if not self.connected:
            return
        try:
            response = await self.client.send_command(
                Command(
                    command=CommandType.SET_VIRTUAL_GAMEPADS,
                    data={"count": int(self.virtual_gamepad_count)},
                )
            )
            if response.status == "ok" and isinstance(response.data, dict):
                data = cast(JsonObject, response.data)
                raw_count = data.get("count", self.virtual_gamepad_count)
                if isinstance(raw_count, (int, float, str)):
                    try:
                        self.virtual_gamepad_count = max(0, int(raw_count))
                    except (TypeError, ValueError, OverflowError):
                        log.warning(
                            "Ignoring malformed virtual gamepad count from keymasqd: %r",
                            raw_count,
                        )
        except OSError as exc:
            log.warning("Failed to configure virtual gamepads in keymasqd: %s", exc)

    async def connect_loop(self: Any) -> None:
        retry_delay = 1.0
        max_delay = 30.0

        while self.running:
            try:
                log.info(f"Connecting to keymasqd at {SOCKET_PATH}")
                await self.client.connect()
                self.connected = True
                retry_delay = 1.0
                log.info("Connected to keymasqd")
                self._broadcast_keymasqd_status(True)
                await self._sync_virtual_gamepads_to_daemon()

                try:
                    await coordinator.activate_initial_profiles(self)
                except Exception:
                    log.exception("Failed to activate initial profiles")
                await recording_lifecycle.sync_pending_macro_slots_from_daemon(self)
                await recording_device_selection.refresh_recording_devices_cache(self)

                await self.client.wait_disconnected()
                log.warning("Disconnected from keymasqd")
                self._handle_keymasqd_disconnect()
                if self.restart_on_daemon_disconnect:
                    log.info("Requesting keymasq-session restart after keymasqd disconnect")
                    self.restart_requested = True
                    self._shutdown_event.set()
                    return

            except OSError as e:
                log.warning(f"Connection failed: {e}")
                self._handle_keymasqd_disconnect()
            except Exception:
                log.exception("Unexpected keymasqd connection loop failure")
                self._handle_keymasqd_disconnect()

            if self.running:
                try:
                    await asyncio.wait_for(self._retry_event.wait(), timeout=retry_delay)
                except TimeoutError:
                    pass
                retry_delay = min(retry_delay * 2, max_delay)

    def _handle_keymasqd_disconnect(self: Any) -> None:
        was_connected = self.connected
        self.connected = False
        self.profile_state.grabbed_devices.clear()
        self.profile_state.grabbed_interfaces.clear()
        self.profile_state.grab_waiting_devices.clear()
        self.profile_state.grab_status.clear()
        self.profile_state.device_runtime_status.clear()
        for task in list(self.profile_state.grab_retry_tasks.values()):
            if not task.done():
                task.cancel()
        self.profile_state.grab_retry_tasks.clear()
        self.profile_state.last_sent_grab_signatures.clear()
        self.profile_state.last_sent_mapping_signatures.clear()
        self.profile_state.last_sent_combo_signature = ""
        self.profile_state.active_profile_names.clear()
        self.profile_state.resolved_devices.clear()
        self.profile_state.resolved_combos.clear()
        self.profile_state.runtime_profile_activations.clear()
        self.device_inspector_state.active_hardware_ids.clear()
        self.device_inspector_state.suppressed_hardware_ids.clear()
        self.device_inspector_state.owners_by_hardware_id.clear()
        self.recording_state.active = False
        self.recording_state.active_slot = 0
        self.recording_state.start_cursor = None
        self.recording_state.active_owner_writer_id = None
        self.recording_state.active_owner_pid = None
        self.recording_state.active_owner_uid = None
        self.recording_state.devices_cache.clear()
        self.recording_state.selected_devices_cache.clear()
        self.recording_state.devices_cache_ready = False
        self.recording_state.devices_cache_include_other = False

        if was_connected:
            self._broadcast_keymasqd_status(False)
