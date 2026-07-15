"""Global-output ownership for a single grab acquisition transaction."""

import logging

import evdev

from keymasq.keymasqd.runtime import adapters, outputs
from keymasq.keymasqd.runtime.grab.state import GrabAcquisitionState, GrabManager


def ensure_global_outputs(
    manager: GrabManager,
    hardware_id: str,
    state: GrabAcquisitionState,
    *,
    log: logging.Logger,
) -> None:
    """Create shared outputs once when this transaction adds the first device."""

    if hardware_id in manager.grabbed_devices or state.created_global_uinputs:
        return
    outputs.create_global_uinputs(
        manager,
        evdev_mod=evdev,  # pyright: ignore[reportArgumentType]
        log=log,
        uinput_writer=adapters.identity_uinput_writer,
    )
    state.created_global_uinputs = True


def destroy_transaction_outputs(
    manager: GrabManager,
    state: GrabAcquisitionState,
    *,
    log: logging.Logger,
) -> None:
    """Destroy outputs only if the current acquisition created their ownership."""

    if state.created_global_uinputs:
        outputs.destroy_global_uinputs(manager, log=log)
