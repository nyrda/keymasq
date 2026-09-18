"""Grabbed gamepad sources are hidden by udev without daemon capabilities.

The daemon writes a flag under /run/keymasq/hidden and asks a bounded
keymasq-hardware@ root job to run `udevadm trigger`; 99-keymasq-hide-grabbed
then clears ID_INPUT_JOYSTICK, marks the node for libinput to ignore, and resets
it to root:root 0600 with only the keymasq ACL. Releasing the source restores
the original policy, and a daemon restart reconciles stale flags first.
"""

from collections.abc import Callable
from pathlib import Path

from support import GAMEPAD_HARDWARE_ID, SOURCE_HIDING_PROFILE_NAME, ScenarioContext

HIDE_TIMEOUT_S = 20.0


def _wait_for[T](
    ctx: ScenarioContext,
    label: str,
    observe: Callable[[], T],
    accept: Callable[[T], bool],
    *,
    node_path: str | None = None,
) -> T:
    """wait_until with the last observed value in the failure message."""
    last: list[T] = []

    def check() -> bool:
        value = observe()
        last[:] = [value]
        return accept(value)

    try:
        ctx.wait_until(label, check, timeout_s=HIDE_TIMEOUT_S)
    except AssertionError as exc:
        raise AssertionError(
            f"{exc}; last observed {last[-1] if last else None!r}\n"
            f"{ctx.node_diagnostics(node_path)}"
        ) from exc
    return last[-1]


def _restricted(ownership: tuple[str, str, int]) -> bool:
    # With the keymasq ACL re-granted, the group bits show the ACL mask, so the
    # node reads as root:root 0660; what matters is that nobody else has access.
    owner, group, mode = ownership
    return owner == "root" and group == "root" and mode & 0o007 == 0


def _expect_hidden(ctx: ScenarioContext, node_path: str, event_name: str) -> None:
    _wait_for(
        ctx,
        f"hidden flag for {event_name}",
        lambda: ctx.hidden_flag_path(event_name).exists(),
        bool,
    )
    _wait_for(
        ctx,
        f"udev hide rule applied to {event_name}",
        lambda: ctx.udev_properties(node_path),
        lambda props: (
            props.get("ID_INPUT_JOYSTICK") is None
            and props.get("LIBINPUT_IGNORE_DEVICE") == "1"
            and ":uaccess:" not in props.get("CURRENT_TAGS", "")
        ),
    )
    _wait_for(
        ctx,
        f"{event_name} restricted to root and the keymasq ACL",
        lambda: ctx.node_ownership(node_path),
        _restricted,
        node_path=node_path,
    )
    _wait_for(
        ctx,
        f"keymasq ACL re-granted on {event_name}",
        lambda: ctx.node_acl_user_permissions(node_path),
        lambda acl: acl.get("keymasq") == "rw",
    )


def _expect_restored(
    ctx: ScenarioContext,
    node_path: str,
    event_name: str,
    original: tuple[str, str, int],
) -> None:
    _wait_for(
        ctx,
        f"hidden flag removed for {event_name}",
        lambda: not ctx.hidden_flag_path(event_name).exists(),
        bool,
    )
    _wait_for(
        ctx,
        f"udev policy restored on {event_name}",
        lambda: ctx.udev_properties(node_path),
        lambda props: (
            props.get("ID_INPUT_JOYSTICK") == "1"
            and props.get("LIBINPUT_IGNORE_DEVICE") is None
            and ":uaccess:" in props.get("CURRENT_TAGS", "")
        ),
    )
    # A change event re-runs the desktop rules (uaccess, the keymasq ACL) but
    # stock udev assigns the input group only on add, so the node keeps the
    # root group until it is recreated. Only the owner and mode are checked.
    _wait_for(
        ctx,
        f"{event_name} owner and mode restored to {original[0]}:{original[2]:o}",
        lambda: ctx.node_ownership(node_path),
        lambda ownership: (ownership[0], ownership[2]) == (original[0], original[2]),
        node_path=node_path,
    )


def run(ctx: ScenarioContext) -> None:
    effective = ctx.daemon_effective_capabilities()
    if effective != 0:
        raise AssertionError(f"keymasqd holds capabilities: CapEff={effective:016x}")

    source = ctx.create_source_gamepad()
    node_path = str(source.device.path)
    event_name = Path(node_path).name
    original = ctx.node_ownership(node_path)
    properties = ctx.udev_properties(node_path)
    if properties.get("ID_INPUT_JOYSTICK") != "1":
        raise AssertionError(f"test gamepad is not classified as a joystick: {properties}")
    if _restricted(original):
        raise AssertionError(f"test gamepad already restricted before the grab: {original}")

    try:
        ctx.write_gamepad_hardware_config()
        ctx.request({"command": "reload"})
        ctx.request({"command": "reevaluate_hardware"})
        ctx.set_profile_enabled(SOURCE_HIDING_PROFILE_NAME, enabled=True)
        ctx.wait_for_hardware_mapping(GAMEPAD_HARDWARE_ID)
        _expect_hidden(ctx, node_path, event_name)

        # The event loop keeps forwarding through the handle opened before the
        # hide; the permission reset must not affect the grabbed source.
        ctx.open_passthrough_output(GAMEPAD_HARDWARE_ID).close()

        # Restart: ExecStartPre recovery and the daemon's own reconcile clear
        # stale flags through root jobs, then the grab hides the source again.
        ctx.restart_keymasqd()
        ctx.wait_for_hardware_mapping(GAMEPAD_HARDWARE_ID)
        _expect_hidden(ctx, node_path, event_name)
        if ctx.daemon_effective_capabilities() != 0:
            raise AssertionError("keymasqd regained capabilities after restart")

        # Removing the hardware config releases the grab immediately (disabling
        # the profile alone defers the hardware release by a minute), which
        # restores the source and clears the model-wide hotplug flag.
        ctx.remove_gamepad_hardware_config()
        ctx.request({"command": "reload"})
        ctx.request({"command": "reevaluate_hardware"})
        _expect_restored(ctx, node_path, event_name, original)
    finally:
        ctx.request(
            {"command": "disable_profile", "profile_name": SOURCE_HIDING_PROFILE_NAME},
            ok=False,
        )
        ctx.remove_gamepad_hardware_config()
        ctx.request({"command": "reload"}, ok=False)
        ctx.request({"command": "reevaluate_hardware"}, ok=False)
        source.close()
        ctx.settle_udev()
