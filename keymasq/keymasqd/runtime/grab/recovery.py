"""Rollback and error reporting for failed grab acquisition transactions."""

import logging
from dataclasses import dataclass

from keymasq.keymasqd.permission_hints import (
    has_permission_hint,
    input_device_permission_message,
    is_permission_error,
)
from keymasq.keymasqd.runtime.grab.outputs import destroy_transaction_outputs
from keymasq.keymasqd.runtime.grab.planning import (
    restore_desired_grab_state,
    store_grabbed_devices,
)
from keymasq.keymasqd.runtime.grab.release import (
    cancel_pending_interface_releases_for_hardware,
)
from keymasq.keymasqd.runtime.grab.state import (
    GrabAcquisitionState,
    GrabManager,
    GrabPlan,
    GrabRequest,
)

log = logging.getLogger("keymasqd.devices")


@dataclass(frozen=True)
class GrabRollbackReport:
    """Observable result of restoring state after a failed acquisition."""

    reported_exception: BaseException
    failed_release_paths: tuple[str, ...] = ()

    @property
    def cleanup_succeeded(self) -> bool:
        return not self.failed_release_paths


async def rollback_failed_grab_report(
    manager: GrabManager,
    request: GrabRequest,
    plan: GrabPlan,
    state: GrabAcquisitionState,
    path: str,
    exc: BaseException,
) -> GrabRollbackReport:
    """Restore the pre-transaction snapshot and report best-effort cleanup errors."""

    reported_exc = permission_aware_grab_exception(path, exc)
    log.error("Failed to grab %s: %s", path, reported_exc)
    failed_release_paths: list[str] = []
    for device in list(state.devices):
        if not state.owns_device(device, plan):
            continue
        try:
            await device.release()
        except Exception:  # noqa: BLE001 - rollback must restore manager state.
            failed_path = str(getattr(device, "path", "<unknown>"))
            failed_release_paths.append(failed_path)
            log.warning(
                "Failed to release %s during grab rollback",
                failed_path,
                exc_info=True,
            )
    store_grabbed_devices(manager, request.hardware_id, plan.existing_devices)
    destroy_transaction_outputs(manager, state, log=log)
    cancel_pending_interface_releases_for_hardware(manager, request.hardware_id)
    if request.update_desired:
        restore_desired_grab_state(
            manager,
            request.hardware_id,
            plan.previous_desired_paths,
            plan.previous_desired_config,
        )
    if failed_release_paths:
        log.warning("Grab rollback completed with cleanup errors")
    return GrabRollbackReport(
        reported_exception=reported_exc,
        failed_release_paths=tuple(failed_release_paths),
    )


def permission_aware_grab_exception(
    path: str,
    exc: BaseException,
) -> BaseException:
    if not is_permission_error(exc) or has_permission_hint(exc):
        return exc
    return PermissionError(
        input_device_permission_message(
            f"Permission denied while opening or grabbing {path}: {exc}"
        )
    )
