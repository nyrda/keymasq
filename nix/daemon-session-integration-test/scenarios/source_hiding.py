"""Grabbed gamepad sources are hidden by udev without daemon capabilities.

The daemon writes a flag under /run/keymasq/hidden and asks a bounded
keymasq-hardware@ root job to run `udevadm trigger`; 99-keymasq-hide-grabbed
then clears ID_INPUT_JOYSTICK, marks the node for libinput to ignore, and resets
it to root:root 0600 with only the keymasq ACL. Releasing the source restores
the original policy, and a daemon restart reconciles stale flags first.
"""

import contextlib
import ctypes
import multiprocessing
import select
import time
from collections.abc import Callable
from pathlib import Path

import evdev
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


def _expect_forwarding(ctx: ScenarioContext, source: evdev.UInput) -> None:
    ctx.drain_outputs()
    for value in (1, 0):
        source.write(evdev.ecodes.EV_KEY, evdev.ecodes.BTN_EAST, value)
        source.syn()
        time.sleep(0.05)
    ctx.expect_keys([(evdev.ecodes.KEY_Y, 1), (evdev.ecodes.KEY_Y, 0)])


def _play_feedback(path: str) -> None:
    with contextlib.closing(evdev.InputDevice(path)) as output:
        effect = evdev.ff.Effect()
        effect.type = evdev.ecodes.FF_RUMBLE
        effect.id = -1
        effect.ff_replay.length = 1000
        effect.u.ff_rumble_effect.strong_magnitude = 0x4000
        effect_id = output.upload_effect(effect)
        output.write(evdev.ecodes.EV_FF, effect_id, 1)
        output.write(evdev.ecodes.EV_FF, effect_id, 0)
        output.erase_effect(effect_id)


def _expect_feedback(ctx: ScenarioContext, source: evdev.UInput) -> None:
    """Round-trip real FF ioctls through the daemon while the source is hidden."""
    with contextlib.closing(ctx.open_passthrough_output(GAMEPAD_HARDWARE_ID)) as output:
        path = output.path
    received = []
    # evdev's blocking upload/erase ioctls hold the GIL. Run the application
    # separately so the emulated physical device can acknowledge its requests.
    client = multiprocessing.get_context("spawn").Process(target=_play_feedback, args=(path,))
    client.start()
    deadline = time.monotonic() + HIDE_TIMEOUT_S
    try:
        while client.is_alive() or received != ["upload", "play", "stop", "erase"]:
            if time.monotonic() >= deadline:
                raise AssertionError(f"force feedback did not complete: {received}")
            if client.exitcode not in (None, 0):
                raise AssertionError(f"force-feedback client failed: {client.exitcode}; {received}")
            if not select.select([source.fd], [], [], 0.05)[0]:
                continue
            for event in source.read():
                if event.type == evdev.ecodes.EV_UINPUT:
                    # Set request_id explicitly. Older evdev wrappers assign it
                    # to the wrong ctypes field in begin_upload/begin_erase.
                    if event.code == evdev.ecodes.UI_FF_UPLOAD:
                        upload = evdev.ff.UInputUpload()
                        upload.request_id = event.value
                        assert source.dll._uinput_begin_upload(source.fd, ctypes.byref(upload)) == 0
                        try:
                            assert upload.effect.type == evdev.ecodes.FF_RUMBLE
                            assert upload.effect.u.ff_rumble_effect.strong_magnitude == 0x4000
                            upload.retval = 0
                            received.append("upload")
                        finally:
                            source.end_upload(upload)
                    elif event.code == evdev.ecodes.UI_FF_ERASE:
                        erase = evdev.ff.UInputErase()
                        erase.request_id = event.value
                        assert source.dll._uinput_begin_erase(source.fd, ctypes.byref(erase)) == 0
                        erase.retval = 0
                        source.end_erase(erase)
                        received.append("erase")
                elif event.type == evdev.ecodes.EV_FF:
                    # Erasing an effect also stops it in the kernel. Accept
                    # repeated stops after the explicit application stop.
                    action = "play" if event.value else "stop"
                    if action != "stop" or received[-1:] != ["stop"]:
                        received.append(action)
        client.join(timeout=1)
        assert client.exitcode == 0, client.exitcode
    except BaseException:
        # Destroying the source also releases any pending kernel FF request.
        source.close()
        raise
    finally:
        if client.is_alive():
            client.terminate()
        client.join(timeout=5)
        if client.is_alive():
            client.kill()
            client.join(timeout=5)


def run(ctx: ScenarioContext) -> None:
    ctx.assert_daemon_has_no_capabilities()

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
        _expect_forwarding(ctx, source)
        _expect_feedback(ctx, source)

        # Restart: ExecStartPre recovery and the daemon's own reconcile clear
        # stale flags through root jobs, then the grab hides the source again.
        ctx.restart_keymasqd()
        ctx.wait_for_hardware_mapping(GAMEPAD_HARDWARE_ID)
        _expect_hidden(ctx, node_path, event_name)
        _expect_forwarding(ctx, source)
        _expect_feedback(ctx, source)
        ctx.assert_daemon_has_no_capabilities()

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
