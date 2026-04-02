import asyncio

from keyforge.session.wayland_protocols.ext_foreign_toplevel_list import (
    ExtForeignToplevelListTracker,
)
from keyforge.session.wayland_protocols.wlr_foreign_toplevel_manager import (
    WLR_TOPLEVEL_STATE_ACTIVATED,
    WlrForeignToplevelManagerTracker,
)


def test_ext_tracker_emits_on_activation() -> None:
    tracker = ExtForeignToplevelListTracker()
    tracker.add_toplevel("a")
    tracker.update_app_id("a", "org.kde.konsole")
    tracker.update_title("a", "Konsole")
    tracker.update_state("a", [2])

    assert tracker.get_active_window() == ("org.kde.konsole", "Konsole")
    assert asyncio.run(tracker.next_active_window(timeout=0.01)) == (
        "org.kde.konsole",
        "Konsole",
    )


def test_ext_tracker_emits_on_active_title_change() -> None:
    tracker = ExtForeignToplevelListTracker()
    tracker.add_toplevel("a")
    tracker.update_app_id("a", "firefox")
    tracker.update_title("a", "A")
    tracker.update_state("a", {"activated": True})
    asyncio.run(tracker.next_active_window(timeout=0.01))

    tracker.update_title("a", "B")
    assert asyncio.run(tracker.next_active_window(timeout=0.01)) == ("firefox", "B")


def test_ext_tracker_emits_on_byte_state_activation() -> None:
    tracker = ExtForeignToplevelListTracker()
    tracker.add_toplevel("x")
    tracker.update_app_id("x", "com.system76.Cosmic")
    tracker.update_title("x", "COSMIC Settings")

    state = (2).to_bytes(4, byteorder="little")
    tracker.update_state("x", state)

    assert tracker.get_active_window() == ("com.system76.Cosmic", "COSMIC Settings")
    assert asyncio.run(tracker.next_active_window(timeout=0.01)) == (
        "com.system76.Cosmic",
        "COSMIC Settings",
    )


def test_wlr_tracker_emits_on_byte_state_activation() -> None:
    tracker = WlrForeignToplevelManagerTracker()
    tracker.add_toplevel("x")
    tracker.update_app_id("x", "Alacritty")
    tracker.update_title("x", "shell")

    state = WLR_TOPLEVEL_STATE_ACTIVATED.to_bytes(4, byteorder="little")
    tracker.update_state("x", state)

    assert tracker.get_active_window() == ("Alacritty", "shell")
    assert asyncio.run(tracker.next_active_window(timeout=0.01)) == ("Alacritty", "shell")


def test_wlr_tracker_switches_focus_between_handles() -> None:
    tracker = WlrForeignToplevelManagerTracker()
    tracker.add_toplevel("a")
    tracker.add_toplevel("b")
    tracker.update_app_id("a", "a-app")
    tracker.update_title("a", "A")
    tracker.update_app_id("b", "b-app")
    tracker.update_title("b", "B")

    tracker.update_state("a", [WLR_TOPLEVEL_STATE_ACTIVATED])
    assert asyncio.run(tracker.next_active_window(timeout=0.01)) == ("a-app", "A")

    tracker.update_state("a", [])
    tracker.update_state("b", [WLR_TOPLEVEL_STATE_ACTIVATED])
    assert asyncio.run(tracker.next_active_window(timeout=0.01)) == ("", "")
    assert asyncio.run(tracker.next_active_window(timeout=0.01)) == ("b-app", "B")
