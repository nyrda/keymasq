"""Ensure daemon-owned outputs are available during grab acquisition."""

import logging

import evdev

from keymasq.keymasqd.runtime import adapters, outputs
from keymasq.keymasqd.runtime.grab.state import GrabManager


def ensure_global_outputs(
    manager: GrabManager,
    *,
    log: logging.Logger,
) -> None:
    """Initialize shared outputs lazily for managers used without a daemon."""
    outputs.create_global_uinputs(
        manager,
        evdev_mod=evdev,  # pyright: ignore[reportArgumentType]
        log=log,
        uinput_writer=adapters.identity_uinput_writer,
    )
