import asyncio
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from keymasq.common.model.profiles import ProfileConfig, WindowRule
from keymasq.session.listeners import gamescope as gamescope_module
from keymasq.session.listeners.gamescope import (
    STEAM_OSK_LAYER,
    STEAM_OVERLAY_LAYER,
    GamescopeListener,
    _decode_display_name,
    _parse_focusable_windows,
)
from keymasq.session.manager.core import SessionManager


async def _noop_cb(_window_class: str, _window_title: str, _tags: list[str]) -> None:
    return


class _FakeWindow:
    def __init__(
        self,
        window_id: int,
        *,
        wm_class: tuple[str, str] | None = None,
        wm_name: str = "",
        props: dict[str, object] | None = None,
        pointer: tuple[int, int] = (0, 0),
        children: list["_FakeWindow"] | None = None,
    ) -> None:
        self.id = window_id
        self._wm_class = wm_class
        self._wm_name = wm_name
        self.props: dict[str, object] = dict(props or {})
        self.pointer = pointer
        self.children: list[_FakeWindow] = list(children or [])
        self.event_mask = 0
        self._atom_names: dict[int, str] = {}

    def bind_atoms(self, atom_names: dict[int, str]) -> None:
        self._atom_names = atom_names

    def change_attributes(self, *, event_mask: int) -> None:
        self.event_mask = event_mask

    def get_full_property(self, atom: int, _property_type: int) -> SimpleNamespace | None:
        name = self._atom_names.get(atom)
        if name is None or name not in self.props:
            return None
        return SimpleNamespace(value=self.props[name])

    def get_wm_class(self) -> tuple[str, str] | None:
        return self._wm_class

    def get_wm_name(self) -> str:
        return self._wm_name

    def query_pointer(self) -> SimpleNamespace:
        return SimpleNamespace(root_x=self.pointer[0], root_y=self.pointer[1])

    def query_tree(self) -> SimpleNamespace:
        return SimpleNamespace(children=list(self.children))


class _FakeDisplay:
    def __init__(
        self,
        name: str,
        root: _FakeWindow,
        windows: dict[int, _FakeWindow],
        fd: int,
    ) -> None:
        self.name = name
        self._root = root
        self._windows = windows
        self._fd = fd
        self._atoms: dict[str, int] = {}
        self._atom_names: dict[int, str] = {}
        self._next_atom = 100
        self.closed = False
        self.events: list[SimpleNamespace] = []
        self.error_handler: object = None
        self._root.bind_atoms(self._atom_names)
        for win in self._windows.values():
            win.bind_atoms(self._atom_names)

    def set_error_handler(self, handler: object) -> None:
        self.error_handler = handler

    def screen(self) -> SimpleNamespace:
        return SimpleNamespace(root=self._root)

    def intern_atom(self, name: str) -> int:
        if name not in self._atoms:
            atom_id = self._next_atom
            self._next_atom += 1
            self._atoms[name] = atom_id
            self._atom_names[atom_id] = name
        return self._atoms[name]

    def sync(self) -> None:
        return

    def close(self) -> None:
        self.closed = True

    def fileno(self) -> int:
        return self._fd

    def pending_events(self) -> int:
        return len(self.events)

    def next_event(self) -> SimpleNamespace:
        return self.events.pop(0)

    def create_resource_object(self, _resource_type: str, resource_id: int) -> _FakeWindow:
        if resource_id in self._windows:
            return self._windows[resource_id]
        raise OSError(f"BadWindow {hex(resource_id)} on {self.name}")


class _FakeDisplayModule:
    def __init__(self, displays: dict[str, _FakeDisplay]) -> None:
        self._displays = displays

    def Display(self, name: str) -> _FakeDisplay:  # noqa: N802
        if name not in self._displays:
            raise OSError(f"Cannot open display {name}")
        return self._displays[name]


def _encode_display_cardinals(display_name: str) -> list[int]:
    raw = display_name.encode("ascii") + b"\x00"
    pad = (-len(raw)) % 4
    raw += b"\x00" * pad
    return list(struct.unpack(f"={len(raw) // 4}I", raw))


def _fake_x_constants() -> SimpleNamespace:
    return SimpleNamespace(
        AnyPropertyType=0,
        NoEventMask=0,
        PropertyChangeMask=1,
        SubstructureNotifyMask=2,
        PropertyNotify=28,
        CreateNotify=16,
        DestroyNotify=17,
        MapNotify=19,
    )


def _build_deck_displays() -> tuple[dict[str, _FakeDisplay], _FakeWindow, _FakeWindow, _FakeWindow]:
    steam_win = _FakeWindow(
        0x2200035,
        wm_class=("steamwebhelper", "steam"),
        wm_name="Steam Big Picture Mode",
        props={
            "_NET_WM_NAME": b"Steam Big Picture Mode",
            "_NET_WM_PID": [267981],
            "STEAM_GAME": [769],
            "STEAM_OVERLAY": [0],
            "STEAM_INPUT_FOCUS": [0],
        },
    )
    mango_win = _FakeWindow(
        0x400007,
        wm_class=("mangoapp overlay window", "mangoapp overlay window"),
        wm_name="mangoapp overlay window",
        props={
            "_NET_WM_NAME": b"mangoapp overlay window",
            "GAMESCOPE_EXTERNAL_OVERLAY": [1],
            "GAMESCOPE_NO_FOCUS": [1],
        },
    )
    root0 = _FakeWindow(
        0x345,
        props={
            "GAMESCOPE_PID": [267522],
            "GAMESCOPE_XWAYLAND_SERVER_ID": [0],
            "GAMESCOPE_FOCUSED_WINDOW": [0x2400007],
            "GAMESCOPE_FOCUSED_APP": [32370],
            "GAMESCOPE_FOCUSED_APP_GFX": [32370],
            "GAMESCOPE_FOCUSABLE_WINDOWS": [
                0x2200035,
                769,
                267981,
                0x2400007,
                32370,
                274489,
            ],
            "GAMESCOPE_FOCUS_DISPLAY": _encode_display_cardinals(":1"),
            "GAMESCOPE_MOUSE_FOCUS_DISPLAY": _encode_display_cardinals(":1"),
            "GAMESCOPE_KEYBOARD_FOCUS_DISPLAY": _encode_display_cardinals(":1"),
        },
        pointer=(1279, 799),
        children=[mango_win, steam_win],
    )
    kotor_win = _FakeWindow(
        0x2400007,
        wm_class=("steam_app_32370", "steam_app_32370"),
        wm_name="Star Wars: Knights of the Old Republic",
        props={
            "_NET_WM_NAME": b"Star Wars: Knights of the Old Republic",
            "_NET_WM_PID": [274489],
            "STEAM_GAME": [32370],
        },
    )
    root1 = _FakeWindow(
        0x346,
        props={
            "GAMESCOPE_PID": [267522],
            "GAMESCOPE_XWAYLAND_SERVER_ID": [1],
        },
        pointer=(400, 299),
        children=[kotor_win],
    )
    disp0 = _FakeDisplay(":0", root0, {0x2200035: steam_win, 0x400007: mango_win}, fd=-1)
    disp1 = _FakeDisplay(":1", root1, {0x2400007: kotor_win}, fd=-1)
    return {":0": disp0, ":1": disp1}, root0, steam_win, kotor_win


def test_decode_display_name_handles_cardinals_and_bytes() -> None:
    assert _decode_display_name(_encode_display_cardinals(":0")) == ":0"
    assert _decode_display_name(_encode_display_cardinals(":1")) == ":1"
    assert _decode_display_name([12602, 0, 83]) == ":1"
    assert _decode_display_name([12346, 0, 81]) == ":0"
    assert _decode_display_name(b":2\x00garbage") == ":2"
    assert _decode_display_name(_encode_display_cardinals("gamescope-0")) == "gamescope-0"
    assert _decode_display_name("gamescope-0") == "gamescope-0"
    assert _decode_display_name(None) == ""


def test_parse_focusable_windows_extracts_triples() -> None:
    parsed = _parse_focusable_windows(
        [
            35651637,
            769,
            267981,
            37748743,
            32370,
            0xFFFFFFFF,
            35651637,
            999,
            999999,
        ]
    )
    assert parsed == {
        35651637: [(769, 267981), (999, 999999)],
        37748743: [(32370, 0)],
    }


def test_nested_gamescope_without_display_env_does_not_hijack_x11(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gs_root = _FakeWindow(
        0x200,
        props={"GAMESCOPE_PID": [9999], "GAMESCOPE_XWAYLAND_SERVER_ID": [0]},
    )
    displays = {":1": _FakeDisplay(":1", gs_root, {}, fd=-1)}
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("GAMESCOPE_WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "XFCE")
    monkeypatch.setenv("XDG_SESSION_DESKTOP", "xfce")
    monkeypatch.setenv("DESKTOP_SESSION", "xfce")
    monkeypatch.setattr(gamescope_module, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(gamescope_module, "xlib_constants", _fake_x_constants())
    monkeypatch.setattr(gamescope_module, "xlib_display", _FakeDisplayModule(displays))
    monkeypatch.setattr(gamescope_module, "_x11_socket_displays", lambda: [":0", ":1"])

    assert GamescopeListener.pick_control_display() is None
    assert asyncio.run(GamescopeListener.probe_available()) is False


def test_nested_gamescope_does_not_hijack_x11_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    xorg_root = _FakeWindow(0x100, props={})
    gs_root = _FakeWindow(
        0x200,
        props={"GAMESCOPE_PID": [9999], "GAMESCOPE_XWAYLAND_SERVER_ID": [0]},
    )
    displays = {
        ":0": _FakeDisplay(":0", xorg_root, {}, fd=-1),
        ":1": _FakeDisplay(":1", gs_root, {}, fd=-1),
    }
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("GAMESCOPE_WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "XFCE")
    monkeypatch.setenv("XDG_SESSION_DESKTOP", "xfce")
    monkeypatch.setenv("DESKTOP_SESSION", "xfce")
    monkeypatch.setattr(gamescope_module, "xlib_constants", _fake_x_constants())
    monkeypatch.setattr(gamescope_module, "xlib_display", _FakeDisplayModule(displays))
    monkeypatch.setattr(gamescope_module, "_x11_socket_displays", lambda: [":0", ":1"])

    assert GamescopeListener.pick_control_display() is None
    assert asyncio.run(GamescopeListener.probe_available()) is False


def test_gamescope_reads_display_from_gamescope_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "gamescope-environment").write_text(
        "GAMESCOPE_WAYLAND_DISPLAY=gamescope-0\nDISPLAY=:0\n",
        encoding="utf-8",
    )
    displays, _root0, _steam, _kotor = _build_deck_displays()
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("GAMESCOPE_WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "gamescope")
    monkeypatch.setattr(gamescope_module, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(gamescope_module, "xlib_constants", _fake_x_constants())
    monkeypatch.setattr(gamescope_module, "xlib_display", _FakeDisplayModule(displays))
    monkeypatch.setattr(gamescope_module, "_x11_socket_displays", lambda: [])

    assert GamescopeListener.pick_control_display() == (":0", 267522)


def test_gamescope_tracks_multi_xwayland_windows_overlays_and_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        displays, root0, steam_win, kotor_win = _build_deck_displays()
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "gamescope")
        monkeypatch.setattr(gamescope_module, "xlib_constants", _fake_x_constants())
        monkeypatch.setattr(gamescope_module, "xlib_display", _FakeDisplayModule(displays))
        monkeypatch.setattr(gamescope_module, "_x11_socket_displays", lambda: [":0", ":1"])
        monkeypatch.setattr(gamescope_module, "_read_gamescope_environment_display", lambda: ":0")

        listener = GamescopeListener(_noop_cb)
        await listener.start()
        try:
            assert await listener.get_active_window() == (
                "steam_app_32370",
                "Star Wars: Knights of the Old Republic",
                [],
            )
            assert displays[":0"].error_handler is gamescope_module._handle_x_protocol_error
            assert displays[":1"].error_handler is gamescope_module._handle_x_protocol_error
            assert listener.active_layer == ""
            assert await listener.get_cursor_position() == (400, 299)

            steam_win.props["STEAM_OVERLAY"] = [1]
            steam_win.props["STEAM_INPUT_FOCUS"] = [1]
            root0.props["GAMESCOPE_FOCUSED_APP"] = [769]
            root0.props["GAMESCOPE_MOUSE_FOCUS_DISPLAY"] = _encode_display_cardinals(":0")
            root0.props["GAMESCOPE_KEYBOARD_FOCUS_DISPLAY"] = _encode_display_cardinals(":0")

            assert await listener.get_active_window() == ("", "", [])
            assert listener.active_layer == STEAM_OVERLAY_LAYER
            assert await listener.get_cursor_position() == (1279, 799)

            steam_win.props["STEAM_INPUT_FOCUS"] = [2]
            root0.props["GAMESCOPE_KEYBOARD_FOCUS_DISPLAY"] = _encode_display_cardinals(":1")

            assert await listener.get_active_window() == ("", "", [])
            assert listener.active_layer == STEAM_OSK_LAYER
            assert await listener.get_cursor_position() == (1279, 799)

            steam_win.props["STEAM_OVERLAY"] = [0]
            steam_win.props["STEAM_INPUT_FOCUS"] = [1]
            root0.props["GAMESCOPE_KEYBOARD_FOCUS_DISPLAY"] = _encode_display_cardinals(":0")

            assert await listener.get_active_window() == ("", "", [])
            assert listener.active_layer == STEAM_OVERLAY_LAYER

            root0.props["GAMESCOPE_FOCUSED_APP_GFX"] = [769]
            root0.props["GAMESCOPE_FOCUSED_WINDOW"] = [0x2200035]
            root0.props["GAMESCOPE_FOCUS_DISPLAY"] = _encode_display_cardinals(":0")

            assert await listener.get_active_window() == (
                "steam",
                "Steam Big Picture Mode",
                [],
            )
            assert listener.active_layer == ""

            steam_win.props["STEAM_INPUT_FOCUS"] = [0]
            root0.props["GAMESCOPE_FOCUSED_APP"] = [32370]
            root0.props["GAMESCOPE_FOCUSED_APP_GFX"] = [32370]
            root0.props["GAMESCOPE_FOCUSED_WINDOW"] = [0x2400007]
            root0.props["GAMESCOPE_FOCUS_DISPLAY"] = _encode_display_cardinals(":1")
            root0.props["GAMESCOPE_MOUSE_FOCUS_DISPLAY"] = _encode_display_cardinals(":1")
            root0.props["GAMESCOPE_KEYBOARD_FOCUS_DISPLAY"] = _encode_display_cardinals(":1")
            kotor_win.props["_NET_WM_NAME"] = b"KOTOR - Updated"

            assert await listener.get_active_window() == (
                "steam_app_32370",
                "KOTOR - Updated",
                [],
            )
            assert listener.active_layer == ""
        finally:
            await listener.stop()

    asyncio.run(run())


def test_gamescope_disambiguates_colliding_xid_across_xwaylands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        win_on_0 = _FakeWindow(
            0x200001,
            wm_class=("steam", "steam"),
            wm_name="Steam Helper",
            props={"_NET_WM_NAME": b"Steam Helper", "_NET_WM_PID": [100], "STEAM_GAME": [769]},
        )
        win_on_1 = _FakeWindow(
            0x200001,
            wm_class=("steam_app_999", "steam_app_999"),
            wm_name="Native Game",
            props={"_NET_WM_NAME": b"Native Game", "_NET_WM_PID": [200], "STEAM_GAME": [999]},
        )
        root0 = _FakeWindow(
            0x345,
            props={
                "GAMESCOPE_PID": [5000],
                "GAMESCOPE_XWAYLAND_SERVER_ID": [0],
                "GAMESCOPE_FOCUSED_WINDOW": [0x200001],
                "GAMESCOPE_FOCUSED_APP": [999],
                "GAMESCOPE_FOCUSED_APP_GFX": [999],
                "GAMESCOPE_FOCUSABLE_WINDOWS": [0x200001, 999, 200, 0x200001, 769, 100],
            },
            children=[win_on_0],
        )
        root1 = _FakeWindow(
            0x346,
            props={"GAMESCOPE_PID": [5000], "GAMESCOPE_XWAYLAND_SERVER_ID": [1]},
            children=[win_on_1],
        )
        displays = {
            ":0": _FakeDisplay(":0", root0, {0x200001: win_on_0}, fd=-1),
            ":1": _FakeDisplay(":1", root1, {0x200001: win_on_1}, fd=-1),
        }
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "gamescope")
        monkeypatch.setattr(gamescope_module, "xlib_constants", _fake_x_constants())
        monkeypatch.setattr(gamescope_module, "xlib_display", _FakeDisplayModule(displays))
        monkeypatch.setattr(gamescope_module, "_x11_socket_displays", lambda: [":0", ":1"])
        monkeypatch.setattr(gamescope_module, "_read_gamescope_environment_display", lambda: ":0")

        listener = GamescopeListener(_noop_cb)
        await listener.start()
        try:
            assert await listener.get_active_window() == ("steam_app_999", "Native Game", [])
        finally:
            await listener.stop()

    asyncio.run(run())


def test_gamescope_wayland_client_falls_back_to_focused_app_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        root0 = _FakeWindow(
            0x345,
            props={
                "GAMESCOPE_PID": [5000],
                "GAMESCOPE_XWAYLAND_SERVER_ID": [0],
                "GAMESCOPE_FOCUSED_WINDOW": [0],
                "GAMESCOPE_FOCUSED_APP": [1091500],
                "GAMESCOPE_FOCUSED_APP_GFX": [1091500],
            },
        )
        displays = {":0": _FakeDisplay(":0", root0, {}, fd=-1)}
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "gamescope")
        monkeypatch.setattr(gamescope_module, "xlib_constants", _fake_x_constants())
        monkeypatch.setattr(gamescope_module, "xlib_display", _FakeDisplayModule(displays))
        monkeypatch.setattr(gamescope_module, "_x11_socket_displays", lambda: [":0"])
        monkeypatch.setattr(gamescope_module, "_read_gamescope_environment_display", lambda: ":0")

        listener = GamescopeListener(_noop_cb)
        await listener.start()
        try:
            assert await listener.get_active_window() == ("steam_app_1091500", "", [])
            assert listener.active_layer == ""
        finally:
            await listener.stop()

    asyncio.run(run())


@pytest.mark.asyncio
async def test_gamescope_overlay_resets_game_profile_to_default() -> None:
    manager = SessionManager()
    manager.profiles.save_profile(
        ProfileConfig(
            name="KotOR",
            enabled=True,
            is_permanent=False,
            window_rules=[WindowRule(field="class", pattern="^steam_app_32370$")],
        )
    )
    manager.profiles.save_profile(
        ProfileConfig(
            name="Steam Overlay",
            enabled=True,
            is_permanent=False,
            window_rules=[WindowRule(field="layer", pattern="^steam-overlay$")],
        )
    )
    manager.compositor_state.compositor_id = "gamescope"
    manager.compositor_state.compositor_capabilities = ["layer_focus"]
    fake_listener = SimpleNamespace(active_layer="")
    manager.compositor_state.window_listener = fake_listener

    await manager.on_window_change("steam_app_32370", "Star Wars: Knights of the Old Republic", [])
    active = [
        p.name
        for p in manager.profiles.resolve_active_profiles(
            manager.compositor_state.current_window,
            manager.compositor_state.compositor_capabilities,
        ).active_profiles
    ]
    assert active == ["Default", "KotOR"]

    fake_listener.active_layer = STEAM_OVERLAY_LAYER
    await manager.on_window_change("", "", [])
    active = [
        p.name
        for p in manager.profiles.resolve_active_profiles(
            manager.compositor_state.current_window,
            manager.compositor_state.compositor_capabilities,
        ).active_profiles
    ]
    assert active == ["Default", "Steam Overlay"]

    fake_listener.active_layer = STEAM_OSK_LAYER
    await manager.on_window_change("", "", [])
    active = [
        p.name
        for p in manager.profiles.resolve_active_profiles(
            manager.compositor_state.current_window,
            manager.compositor_state.compositor_capabilities,
        ).active_profiles
    ]
    assert active == ["Default"]

    fake_listener.active_layer = ""
    await manager.on_window_change("steam_app_32370", "Star Wars: Knights of the Old Republic", [])
    active = [
        p.name
        for p in manager.profiles.resolve_active_profiles(
            manager.compositor_state.current_window,
            manager.compositor_state.compositor_capabilities,
        ).active_profiles
    ]
    assert active == ["Default", "KotOR"]


def test_gamescope_ignores_no_focus_overlay_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        displays, _root0, steam_win, _kotor = _build_deck_displays()
        steam_win.props["STEAM_OVERLAY"] = [1]
        steam_win.props["STEAM_INPUT_FOCUS"] = [1]
        steam_win.props["GAMESCOPE_NO_FOCUS"] = [1]
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "gamescope")
        monkeypatch.setattr(gamescope_module, "xlib_constants", _fake_x_constants())
        monkeypatch.setattr(gamescope_module, "xlib_display", _FakeDisplayModule(displays))
        monkeypatch.setattr(gamescope_module, "_x11_socket_displays", lambda: [":0", ":1"])
        monkeypatch.setattr(gamescope_module, "_read_gamescope_environment_display", lambda: ":0")

        listener = GamescopeListener(_noop_cb)
        await listener.start()
        try:
            assert await listener.get_active_window() == (
                "steam_app_32370",
                "Star Wars: Knights of the Old Republic",
                [],
            )
            assert listener.active_layer == ""
        finally:
            await listener.stop()

    asyncio.run(run())


def test_gamescope_re_registers_reader_when_sibling_disconnects_and_reuses_fd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        displays, _root0, _steam, kotor_win = _build_deck_displays()
        disp1_old = displays[":1"]
        disp1_old._fd = 42

        class _FakeLoop:
            def __init__(self) -> None:
                self.added: list[int] = []
                self.removed: list[int] = []

            def add_reader(self, fd: int, _cb: object) -> None:
                self.added.append(fd)

            def remove_reader(self, fd: int) -> None:
                self.removed.append(fd)

        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "gamescope")
        monkeypatch.setattr(gamescope_module, "xlib_constants", _fake_x_constants())
        monkeypatch.setattr(gamescope_module, "xlib_display", _FakeDisplayModule(displays))
        monkeypatch.setattr(gamescope_module, "_x11_socket_displays", lambda: [":0", ":1"])
        monkeypatch.setattr(gamescope_module, "_read_gamescope_environment_display", lambda: ":0")

        listener = GamescopeListener(_noop_cb)
        fake_loop = _FakeLoop()
        listener._control_display_name = ":0"
        listener._gamescope_pid = 267522
        listener._open_control_display()
        listener._loop = fake_loop  # type: ignore[assignment]

        assert await listener.get_active_window() == (
            "steam_app_32370",
            "Star Wars: Knights of the Old Republic",
            [],
        )
        assert fake_loop.added == [42]

        def _disconnect() -> tuple[str, str] | None:
            raise ConnectionResetError("peer closed")

        kotor_win.get_wm_class = _disconnect  # type: ignore[method-assign]
        assert await listener.get_active_window() == ("steam_app_32370", "", [])
        assert ":1" not in listener._siblings
        assert fake_loop.removed == [42]

        new_kotor = _FakeWindow(
            0x2400007,
            wm_class=("steam_app_32370", "steam_app_32370"),
            wm_name="KOTOR Reconnected",
            props={
                "_NET_WM_NAME": b"KOTOR Reconnected",
                "_NET_WM_PID": [274489],
                "STEAM_GAME": [32370],
            },
        )
        new_root1 = _FakeWindow(
            0x346,
            props={"GAMESCOPE_PID": [267522], "GAMESCOPE_XWAYLAND_SERVER_ID": [1]},
            children=[new_kotor],
        )
        displays[":1"] = _FakeDisplay(":1", new_root1, {0x2400007: new_kotor}, fd=42)

        assert await listener.get_active_window() == (
            "steam_app_32370",
            "KOTOR Reconnected",
            [],
        )
        assert fake_loop.added == [42, 42]
        listener._clear_loop_readers()
        listener._close_all_displays()

    asyncio.run(run())


def test_stale_inherited_display_falls_back_to_gamescope_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "gamescope-environment").write_text("DISPLAY=:1\n", encoding="utf-8")
    gs_root = _FakeWindow(
        0x200,
        props={"GAMESCOPE_PID": [267522], "GAMESCOPE_XWAYLAND_SERVER_ID": [0]},
    )
    displays = {":1": _FakeDisplay(":1", gs_root, {}, fd=-1)}
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("GAMESCOPE_WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setenv("XDG_SESSION_DESKTOP", "plasma")
    monkeypatch.setenv("DESKTOP_SESSION", "plasma")
    monkeypatch.setattr(gamescope_module, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(gamescope_module, "xlib_constants", _fake_x_constants())
    monkeypatch.setattr(gamescope_module, "xlib_display", _FakeDisplayModule(displays))
    monkeypatch.setattr(gamescope_module, "_x11_socket_displays", lambda: [":0", ":1"])

    assert GamescopeListener.pick_control_display() == (":1", 267522)

    xorg_root = _FakeWindow(0x100, props={})
    displays[":0"] = _FakeDisplay(":0", xorg_root, {}, fd=-1)
    assert GamescopeListener.pick_control_display() is None


def test_gamescope_preserves_metadata_when_xres_pid_is_unknown_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        displays, root0, _steam, _kotor = _build_deck_displays()
        root0.props["GAMESCOPE_FOCUSABLE_WINDOWS"] = [
            0x2200035,
            769,
            0xFFFFFFFF,
            0x2400007,
            32370,
            0xFFFFFFFF,
        ]
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "gamescope")
        monkeypatch.setattr(gamescope_module, "xlib_constants", _fake_x_constants())
        monkeypatch.setattr(gamescope_module, "xlib_display", _FakeDisplayModule(displays))
        monkeypatch.setattr(gamescope_module, "_x11_socket_displays", lambda: [":0", ":1"])
        monkeypatch.setattr(gamescope_module, "_read_gamescope_environment_display", lambda: ":0")

        listener = GamescopeListener(_noop_cb)
        await listener.start()
        try:
            assert await listener.get_active_window() == (
                "steam_app_32370",
                "Star Wars: Knights of the Old Republic",
                [],
            )
            assert listener._active_watch_window_id == 0x2400007
        finally:
            await listener.stop()

    asyncio.run(run())


def test_gamescope_cursor_returns_none_for_wayland_or_unopenable_focus_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        displays, root0, _steam, _kotor = _build_deck_displays()
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "gamescope")
        monkeypatch.setattr(gamescope_module, "xlib_constants", _fake_x_constants())
        monkeypatch.setattr(gamescope_module, "xlib_display", _FakeDisplayModule(displays))
        monkeypatch.setattr(gamescope_module, "_x11_socket_displays", lambda: [":0", ":1"])
        monkeypatch.setattr(gamescope_module, "_read_gamescope_environment_display", lambda: ":0")

        listener = GamescopeListener(_noop_cb)
        await listener.start()
        try:
            root0.props["GAMESCOPE_MOUSE_FOCUS_DISPLAY"] = _encode_display_cardinals("gamescope-0")
            assert await listener.get_cursor_position() is None

            root0.props["GAMESCOPE_MOUSE_FOCUS_DISPLAY"] = _encode_display_cardinals(":2")
            assert await listener.get_cursor_position() is None
        finally:
            await listener.stop()

    asyncio.run(run())
