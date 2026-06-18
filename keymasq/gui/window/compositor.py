# pyright: reportUnusedFunction=false

from __future__ import annotations

from keymasq.common.models import HardwareConfig

from . import _runtime, device_tabs, gnome_setup


def _probe_startup_state(window) -> tuple[dict[str, object], list[HardwareConfig]]:
    if window.demo_mode:
        return (
            {
                "compositor_id": None,
                "support_details": {"supported": False, "warning": ""},
                "supported": False,
                "capabilities": [],
            },
            [],
        )

    compositor_id = _runtime.detect_compositor_sync()
    support_details = _runtime.get_compositor_support_details_sync(compositor_id)
    supported = bool(support_details.get("supported", False))
    if compositor_id != "gnome":
        supported = _runtime.is_compositor_supported_sync(compositor_id)
    return (
        {
            "compositor_id": compositor_id,
            "support_details": support_details,
            "supported": supported,
            "capabilities": _runtime.get_compositor_capabilities(compositor_id),
        },
        window.hardware_manager.list_hardware(),
    )


def _start_startup_probe(window) -> None:
    if window.demo_mode:

        def on_demo_probe_finished(
            result: _runtime.GuiTaskResult[tuple[dict[str, object], list[HardwareConfig]]],
        ) -> bool:
            return _on_startup_probe_finished(window, result)

        _runtime.GLib.idle_add(
            on_demo_probe_finished,
            _runtime.GuiTaskResult(value=_probe_startup_state(window)),
        )
        return

    _runtime.run_gui_task(
        lambda: _probe_startup_state(window),
        lambda result: _on_startup_probe_finished(window, result),
    )


def _on_startup_probe_finished(
    window,
    result: _runtime.GuiTaskResult[tuple[dict[str, object], list[HardwareConfig]]],
) -> bool:
    if window._destroyed:
        return False
    if result.ok and result.value is not None:
        compositor_state, devices = result.value
    else:
        compositor_state, devices = ({}, [])
    window._startup_probe_done = True
    _apply_compositor_state(window, compositor_state)
    device_tabs._apply_loaded_devices(window, devices)
    return False


def _apply_compositor_state(window, state: dict[str, object]) -> None:
    compositor_id = state.get("compositor_id")
    window._compositor_id = compositor_id if isinstance(compositor_id, str) else None
    details = state.get("support_details")
    window._compositor_support_details = (
        details
        if isinstance(details, dict)
        else {
            "supported": False,
            "warning": "",
        }
    )
    window._compositor_supported = bool(state.get("supported", False))
    caps = state.get("capabilities")
    window._compositor_capabilities = list(caps) if isinstance(caps, list) else []
    if window.combo_tab is not None:
        window.combo_tab._compositor_capabilities = window._compositor_capabilities
        window.combo_tab.refresh_profiles(
            preferred_profile_name=window._selected_profile_name,
            publish_selection=False,
        )
    _update_compositor_warning_banner(window)
    _update_compositor_status(window)
    gnome_setup._close_gnome_setup_dialog_if_ready(window)
    gnome_setup._maybe_present_gnome_setup_dialog(window)


def _update_compositor_dispatch_state(window, status_data: dict | None) -> None:
    if isinstance(status_data, dict) and status_data.get("status") == "ok":
        window._listener_name = str(status_data.get("listener_name", "") or "")
        window._compositor_dispatch_available = bool(
            status_data.get("compositor_dispatch_available", False)
        )
        compositor_id = status_data.get("compositor_id")
        if compositor_id is not None:
            window._compositor_id = compositor_id
        return

    window._listener_name = ""
    window._compositor_dispatch_available = False


def get_compositor_action_status(window) -> dict[str, object]:
    return {
        "compositor_id": window._compositor_id,
        "listener_name": window._listener_name,
        "compositor_dispatch_available": window._compositor_dispatch_available,
    }


def _update_compositor_status(window) -> None:
    if not window._startup_probe_done:
        window.compositor_status.set_label("compositor: ⚪ checking")
        window.compositor_status.set_tooltip_text("Checking compositor support")
        return
    if window._compositor_supported:
        icon = "🟢"
        name = _runtime.get_compositor_name(window._compositor_id)
        window.compositor_status.set_label(f"compositor: {icon} {name}")
        window.compositor_status.set_tooltip_text(f"{name} support is active")
    elif window._compositor_id:
        icon = "🟡"
        name = _runtime.get_compositor_name(window._compositor_id)
        window.compositor_status.set_label(f"compositor: {icon} {name} (limited)")
        if window._compositor_id == "gnome" and gnome_setup._gnome_setup_needed(window):
            window.compositor_status.set_tooltip_text("Click to set up the GNOME Shell bridge")
        else:
            window.compositor_status.set_tooltip_text(f"{name} support is limited")
    else:
        window.compositor_status.set_label("compositor: 🔴 none")
        window.compositor_status.set_tooltip_text("No supported compositor detected")


def _update_compositor_warning_banner(window) -> None:
    if window.demo_mode or not window._startup_probe_done:
        window.warning_banner.set_visible(False)
        window.warning_banner.set_revealed(False)
        return

    if window._compositor_id == "gnome":
        window.warning_banner.set_visible(False)
        window.warning_banner.set_revealed(False)
        return

    compositor_name = _runtime.get_compositor_name(window._compositor_id)
    msg = str(window._compositor_support_details.get("warning", "") or "")
    if msg:
        window.warning_banner.set_title(msg)
        window.warning_banner.set_visible(True)
        window.warning_banner.set_revealed(True)
        return

    if window._compositor_supported:
        window.warning_banner.set_visible(False)
        window.warning_banner.set_revealed(False)
        return

    if window._compositor_id:
        msg = (
            f"⚠️ Compositor '{compositor_name}' has limited support. "
            "Window rules are unavailable on this setup."
        )
    else:
        msg = "⚠️ No supported compositor detected. Window rules are unavailable."
    window.warning_banner.set_title(msg)
    window.warning_banner.set_visible(True)
    window.warning_banner.set_revealed(True)
