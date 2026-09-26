# ruff: noqa: E402
from collections.abc import Callable
from typing import Any

import pytest

gi = pytest.importorskip("gi")

from keymasq.common.model.actions import MappingAction
from keymasq.common.model.core import ActionType
from keymasq.common.model.hardware import ButtonDefinition, HardwareConfig
from keymasq.common.model.profiles import (
    DeviceProfileLayer,
    ProfileConfig,
    RolloverGroup,
    RolloverMember,
    RolloverWinner,
)
from keymasq.gui.widgets.device_tab.rollover import RolloverWindowState
from keymasq.gui.widgets.device_tab.rollover_dialog import (
    RolloverDialogCallbacks,
    RolloverGroupDialog,
    RolloverMemberInfo,
)
from keymasq.gui.widgets.device_tab.rollover_state import (
    RolloverSelection,
    axis_display_name,
    rollover_member_caption,
    rollover_member_tooltip,
    rollover_mode_summary,
    selection_block_reason,
    selection_summary,
    suggest_axis_rollover_members,
)
from tests.gui.support import collect_widgets

HARDWARE_ID = "1234:5678"
MOUSE_ID = "046d:c52b"


def kb(button: str) -> RolloverMember:
    return RolloverMember(hardware_id=HARDWARE_ID, button=button)


def side() -> RolloverMember:
    return RolloverMember(hardware_id=MOUSE_ID, button="btn_side")


def _axis(value: int, target: str = "abs_x") -> MappingAction:
    return MappingAction(action_type=ActionType.GAMEPAD_AXIS, target=target, axis_value=value)


def _device() -> HardwareConfig:
    return HardwareConfig(
        vendor_id="1234",
        product_id="5678",
        name="Keypad",
        evdev_devices=[],
        buttons=[
            ButtonDefinition(id="key_a", label="A", evdev="key_a"),
            ButtonDefinition(id="key_d", label="D", evdev="key_d"),
            ButtonDefinition(id="key_w", label="W", evdev="key_w"),
            ButtonDefinition(id="key_m", label="M", evdev="key_m"),
        ],
    )


def _mouse() -> HardwareConfig:
    return HardwareConfig(
        vendor_id="046d",
        product_id="c52b",
        name="Mouse",
        evdev_devices=[],
        buttons=[ButtonDefinition(id="btn_side", label="Side", evdev="btn_side")],
    )


class _FakePage:
    def __init__(self, child: Any) -> None:
        self.child = child

    def get_child(self) -> Any:
        return self.child


class _FakeTabView:
    def __init__(self) -> None:
        self.pages: list[_FakePage] = []
        self.selected: _FakePage | None = None

    def get_n_pages(self) -> int:
        return len(self.pages)

    def get_nth_page(self, index: int) -> _FakePage:
        return self.pages[index]

    def set_selected_page(self, page: _FakePage) -> None:
        self.selected = page


class _FakeWindow:
    """The parts of the main window that device tabs use for rollover state."""

    def __init__(self) -> None:
        self.rollover_state = RolloverWindowState()
        self.tab_view = _FakeTabView()
        self.placeholder = None
        self.combo_tab = None
        self._device_pages: dict[str, _FakePage] = {}
        self._selected_profile_name = "Gaming"
        self._syncing_profile_selection = False

    def add(self, tab: Any) -> None:
        page = _FakePage(tab)
        self.tab_view.pages.append(page)
        self._device_pages[tab.device.hardware_id] = page


def _profile_manager(
    mappings: dict[str, MappingAction] | None = None,
    groups: list[RolloverGroup] | None = None,
    mouse_mappings: dict[str, MappingAction] | None = None,
) -> Any:
    from keymasq.session.profile.manager import ProfileManager

    layers = {
        HARDWARE_ID: DeviceProfileLayer(hardware_id=HARDWARE_ID, mappings=dict(mappings or {}))
    }
    if mouse_mappings is not None:
        layers[MOUSE_ID] = DeviceProfileLayer(hardware_id=MOUSE_ID, mappings=dict(mouse_mappings))
    profile_manager = ProfileManager()
    profile_manager.save_profile(
        ProfileConfig(
            name="Gaming",
            enabled=True,
            device_layers=layers,
            rollover_groups=list(groups or []),
        )
    )
    return profile_manager


def _device_tab(
    monkeypatch: pytest.MonkeyPatch,
    device: HardwareConfig,
    profile_manager: Any,
    window: _FakeWindow | None = None,
) -> Any:
    from keymasq.gui.widgets.device_tab.tab import DeviceTab

    tab = DeviceTab(
        device=device,
        profile_manager=profile_manager,
        main_window=window,
        demo_mode=False,
    )
    monkeypatch.setattr(tab, "_request_session_async", lambda _payload, _callback: None)
    if window is not None:
        window.add(tab)
    return tab


def _tab(
    monkeypatch: pytest.MonkeyPatch,
    mappings: dict[str, MappingAction] | None = None,
    groups: list[RolloverGroup] | None = None,
) -> Any:
    return _device_tab(monkeypatch, _device(), _profile_manager(mappings, groups))


def _stored_groups() -> list[RolloverGroup]:
    from keymasq.session.profile.manager import ProfileManager

    profile = ProfileManager().get_profile("Gaming")
    assert profile is not None
    return profile.config.rollover_groups


def _action_text(tab: Any, button_id: str) -> str:
    label = tab._button_widgets[button_id]._action_label
    return label.get_tooltip_text() or label.get_text()


class _FakeAlert:
    instances: list["_FakeAlert"] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.handlers: list[Callable[..., None]] = []
        _FakeAlert.instances.append(self)

    def add_response(self, *_args: object) -> None: ...

    def set_response_appearance(self, *_args: object) -> None: ...

    def set_default_response(self, *_args: object) -> None: ...

    def connect(self, _signal: str, handler: Callable[..., None]) -> None:
        self.handlers.append(handler)

    def present(self, *_args: object) -> None: ...

    def respond(self, response: str) -> None:
        for handler in self.handlers:
            handler(self, response)


def test_suggestion_finds_ungrouped_keys_on_one_axis() -> None:
    mappings = {
        "key_d": _axis(32767),
        "key_a": _axis(-32768),
        "key_w": _axis(-32768, "abs_y"),
        "key_m": _axis(-32768, "abs_y"),
    }
    order = ["key_a", "key_d", "key_w", "key_m"]

    assert suggest_axis_rollover_members(mappings, set(), order) == ["key_a", "key_d"]
    # key_w and key_m share a value, so releasing either leaves nothing to fix.
    assert suggest_axis_rollover_members(mappings, {"key_a", "key_d"}, order) is None


def test_selection_blocks_protected_and_grouped_cells() -> None:
    group = RolloverGroup(name="Strafe", members=[kb("key_a"), side()])

    assert selection_block_reason(kb("btn_left"), [], None) is not None
    assert selection_block_reason(side(), [group], None) == (
        "Already in the rollover group Strafe."
    )
    assert selection_block_reason(side(), [group], group) is None
    assert selection_block_reason(RolloverMember(MOUSE_ID, "key_a"), [group], None) is None
    selection = RolloverSelection(profile_name="Gaming")
    selection.toggle(kb("key_a"))
    assert not selection.can_finish
    selection.toggle(side())
    assert selection.can_finish
    selection.toggle(kb("key_a"))
    assert selection.selected == [side()]


def test_selection_summary_groups_keys_by_device() -> None:
    assert selection_summary([], "Keypad").startswith("Click keys to add them.")
    assert selection_summary([("Keypad", ["A", "D"])], "Keypad") == "Selected: A, D"
    assert selection_summary([("Keypad", ["A", "D"])], "Mouse") == "Selected: A, D on Keypad"
    assert selection_summary([("Keypad", ["A"]), ("Mouse", ["Side"])], "Keypad") == (
        "Selected: A on Keypad; Side on Mouse"
    )


def test_mode_summary_and_caption() -> None:
    group = RolloverGroup(
        name="A / D",
        members=[kb("key_a"), kb("key_d")],
        winner=RolloverWinner.OLDEST,
    )
    assert rollover_mode_summary(group) == "First pressed wins, returns to held keys"
    assert rollover_member_caption("🎮 Left Stick X -32768") == "↹ Left Stick X -32768"
    assert rollover_member_caption("→ RB") == "↹ RB"
    assert rollover_member_caption("key_a") == "↹ key_a"
    assert rollover_member_tooltip(group, "→ X").startswith(
        "→ X\nRollover group A / D: First pressed wins"
    )
    assert axis_display_name("abs_x") == "Left Stick X"
    assert axis_display_name("abs_hat0x") == "ABS_HAT0X"


def test_selection_mode_creates_a_group_and_opens_the_editor(
    temp_config_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tab = _tab(monkeypatch, {"key_a": _axis(-32768), "key_d": _axis(32767)})
    opened: list[RolloverGroup] = []
    monkeypatch.setattr(tab, "_open_rollover_group_editor", opened.append)
    buttons = {button.id: button for button in tab.device.buttons}

    tab._on_rollover_group_clicked(None)
    assert tab.rollover_selection_revealer.get_reveal_child() is True
    assert tab.rollover_finish_button.get_sensitive() is False

    tab._activate_mapping_button(buttons["key_a"], False)
    tab._activate_mapping_button(buttons["key_d"], False)
    assert tab._button_widgets["key_a"].has_css_class("rollover-selected")
    assert tab.rollover_finish_button.get_label() == "Create"
    assert tab.rollover_finish_button.get_sensitive() is True

    tab._on_rollover_selection_finish_clicked(None)

    expected = RolloverGroup(name="A / D", members=[kb("key_a"), kb("key_d")])
    assert _stored_groups() == [expected]
    assert opened == [expected]
    assert tab._rollover_selection is None
    assert tab.rollover_selection_revealer.get_reveal_child() is False
    assert tab.rollover_selection_revealer.get_visible() is False
    assert not tab._button_widgets["key_a"].has_css_class("rollover-selected")
    caption = tab._button_widgets["key_a"]._action_label.get_text()
    assert caption.startswith("↹ ")
    assert "Rollover group A / D" in _action_text(tab, "key_a")


def test_selection_ignores_blocked_cells_and_escape_cancels(
    temp_config_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gi.repository import Gdk

    tab = _tab(
        monkeypatch,
        {"key_m": MappingAction(action_type=ActionType.MACRO, macro_name="spin")},
        [RolloverGroup(name="Strafe", members=[kb("key_a"), kb("key_d")])],
    )
    buttons = {button.id: button for button in tab.device.buttons}

    tab._start_rollover_selection()
    tab._activate_mapping_button(buttons["key_a"], False)
    tab._activate_mapping_button(buttons["key_m"], False)

    # Any mapping can join. Only a member of another group is blocked.
    assert tab._rollover_selection.selected == [kb("key_m")]
    widget = tab._button_widgets["key_a"]
    assert widget.has_css_class("rollover-unavailable")
    assert widget.get_tooltip_text() == "Already in the rollover group Strafe."

    # A profile refresh keeps selection mode, a switch to another profile ends it.
    tab._after_profile_selection_applied()
    assert tab._rollover_selection is not None
    tab._rollover_selection.profile_name = "Other"
    tab._after_profile_selection_applied()
    assert tab._rollover_selection is None

    tab._start_rollover_selection()
    assert tab._on_rollover_key_pressed(None, Gdk.KEY_Escape, 0, 0) is True
    assert tab._rollover_selection is None
    assert not widget.has_css_class("rollover-unavailable")


def test_member_click_opens_editor_and_change_members_edits_in_place(
    temp_config_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = RolloverGroup(name="Strafe", members=[kb("key_a"), kb("key_d")])
    tab = _tab(monkeypatch, {"key_a": _axis(-32768), "key_d": _axis(32767)}, [group])
    opened: list[RolloverGroup] = []
    monkeypatch.setattr(tab, "_open_rollover_group_editor", opened.append)
    buttons = {button.id: button for button in tab.device.buttons}

    tab._activate_mapping_button(buttons["key_d"], False)
    assert opened == [group]

    tab._start_rollover_selection(editing=group)
    assert tab.rollover_finish_button.get_label() == "Save"
    tab._activate_mapping_button(buttons["key_w"], False)
    tab._activate_mapping_button(buttons["key_a"], False)
    tab._finish_rollover_selection()

    assert _stored_groups() == [RolloverGroup(name="Strafe", members=[kb("key_d"), kb("key_w")])]


def test_banner_suggests_a_group_for_keys_on_one_axis(
    temp_config_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tab = _tab(monkeypatch, {"key_a": _axis(-32768), "key_d": _axis(32767)})

    assert tab.rollover_banner.get_reveal_child() is True
    assert tab.rollover_banner_label.get_text().startswith("A and D both drive Left Stick X.")

    tab._on_rollover_banner_clicked(None)

    assert tab._rollover_selection.selected == [kb("key_a"), kb("key_d")]
    assert tab.rollover_banner.get_reveal_child() is False
    assert tab.rollover_banner.get_visible() is False


def _dialog(group: RolloverGroup, saved: list[RolloverGroup], deleted: list[bool]) -> Any:
    def save(updated: RolloverGroup) -> bool:
        saved.append(updated)
        return True

    return RolloverGroupDialog(
        group,
        RolloverDialogCallbacks(
            describe_member=lambda member: RolloverMemberInfo(
                label=member.button.upper(),
                device="Keypad",
                mapping="→ X",
            ),
            save=save,
            delete=lambda: deleted.append(True),
            change_members=lambda: None,
            edit_mapping=lambda _member: None,
        ),
    )


def test_editor_saves_winner_restore_and_priority_order() -> None:
    saved: list[RolloverGroup] = []
    dialog = _dialog(RolloverGroup(name="G", members=[kb("a"), kb("d")]), saved, [])

    dialog.winner_row.set_selected(3)
    assert saved[-1].winner is RolloverWinner.PRIORITY
    assert dialog.winner_row.get_subtitle() == (
        "The held key highest in the member list is active."
    )

    dialog._on_move_clicked(None, kb("d"), -1)
    assert saved[-1].members == [kb("d"), kb("a")]
    assert dialog._member_rows[0].get_subtitle() == "Keypad · → X"

    dialog.restore_row.set_active(False)
    assert saved[-1].restore is False
    assert saved[-1].members == [kb("d"), kb("a")]

    dialog.name_row.set_text("Hitbox")
    dialog._on_name_applied(dialog.name_row)
    assert saved[-1].name == "Hitbox"


@pytest.mark.parametrize(
    ("mappings", "revealed"),
    [
        ({"key_a": _axis(-32768), "key_d": _axis(32767)}, False),
        (
            {
                "key_a": _axis(-32768),
                "key_d": MappingAction(action_type=ActionType.KEYBOARD, target="key_right"),
            },
            True,
        ),
        ({"key_a": _axis(-32768)}, True),
    ],
    ids=["axis-only", "keyboard-mapping", "unmapped-key-passes-through"],
)
def test_editor_warns_about_anti_cheat_when_a_member_sends_keys(
    temp_config_dir,
    monkeypatch: pytest.MonkeyPatch,
    mappings: dict[str, MappingAction],
    revealed: bool,
) -> None:
    group = RolloverGroup(name="Strafe", members=[kb("key_a"), kb("key_d")])
    tab = _tab(monkeypatch, mappings, [group])

    tab._open_rollover_group_editor(group)

    dialog = tab._rollover_ui_state().dialog
    assert dialog is not None
    assert dialog.anti_cheat_note.get_visible() is revealed


def test_dismissed_anti_cheat_warning_stays_hidden(
    temp_config_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gi.repository import Gtk

    keys = MappingAction(action_type=ActionType.KEYBOARD, target="key_right")
    group = RolloverGroup(name="Strafe", members=[kb("key_a"), kb("key_d")])
    tab = _tab(monkeypatch, {"key_a": keys, "key_d": keys}, [group])

    def open_note() -> Any:
        tab._open_rollover_group_editor(group)
        return tab._rollover_ui_state().dialog.anti_cheat_note

    note = open_note()
    assert note.get_visible() is True
    collect_widgets(note, Gtk.Button)[-1].emit("clicked")
    assert note.get_visible() is False
    assert open_note().get_visible() is False


def test_editor_removing_below_two_members_asks_to_delete(monkeypatch) -> None:
    from keymasq.gui.widgets.device_tab import rollover_dialog

    monkeypatch.setattr(rollover_dialog.Adw, "AlertDialog", _FakeAlert)
    _FakeAlert.instances.clear()
    saved: list[RolloverGroup] = []
    deleted: list[bool] = []
    dialog = _dialog(RolloverGroup(name="G", members=[kb("a"), kb("d"), kb("w")]), saved, deleted)

    dialog._on_remove_clicked(None, kb("w"))
    assert saved[-1].members == [kb("a"), kb("d")]
    assert _FakeAlert.instances == []

    dialog._on_remove_clicked(None, kb("a"))
    assert len(saved) == 1
    _FakeAlert.instances[-1].respond("delete")
    assert deleted == [True]


def _two_device_window(
    monkeypatch: pytest.MonkeyPatch,
    groups: list[RolloverGroup] | None = None,
) -> tuple[_FakeWindow, Any, Any]:
    window = _FakeWindow()
    profile_manager = _profile_manager(
        {"key_a": _axis(-32768)},
        groups,
        mouse_mappings={"btn_side": _axis(32767)},
    )
    keypad = _device_tab(monkeypatch, _device(), profile_manager, window)
    mouse = _device_tab(monkeypatch, _mouse(), profile_manager, window)
    return window, keypad, mouse


def test_selection_stays_active_across_device_tabs(
    temp_config_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gi.repository import Gdk

    _window, keypad, mouse = _two_device_window(monkeypatch)
    opened: list[RolloverGroup] = []
    monkeypatch.setattr(mouse, "_open_rollover_group_editor", opened.append)

    keypad._on_rollover_group_clicked(None)
    keypad._activate_mapping_button(keypad._button_by_id("key_a"), False)

    # The other tab shows the same selection and adds its own keys to it.
    assert mouse.rollover_selection_revealer.get_reveal_child() is True
    assert keypad.rollover_selection_label.get_text() == "Selected: A"
    assert mouse.rollover_selection_label.get_text() == "Selected: A on Keypad"
    mouse._activate_mapping_button(mouse._button_by_id("btn_side"), False)
    for tab in (keypad, mouse):
        assert tab.rollover_selection_label.get_text() == "Selected: A on Keypad; Side on Mouse"
        assert tab.rollover_finish_button.get_sensitive() is True
    assert mouse._button_widgets["btn_side"].has_css_class("rollover-selected")
    assert not keypad._button_widgets["key_d"].has_css_class("rollover-selected")

    mouse._on_rollover_selection_finish_clicked(None)

    expected = RolloverGroup(name="A / Side", members=[kb("key_a"), side()])
    assert _stored_groups() == [expected]
    assert opened == [expected]
    for tab, button_id in ((keypad, "key_a"), (mouse, "btn_side")):
        assert tab.rollover_selection_revealer.get_reveal_child() is False
        assert tab._button_widgets[button_id]._action_label.get_text().startswith("↹ ")

    # Escape in either tab ends the shared selection.
    mouse._start_rollover_selection()
    assert keypad.rollover_selection_revealer.get_reveal_child() is True
    assert keypad._on_rollover_key_pressed(None, Gdk.KEY_Escape, 0, 0) is True
    assert mouse._rollover_selection is None
    assert mouse.rollover_selection_revealer.get_reveal_child() is False


def test_editor_describes_and_edits_members_on_other_devices(
    temp_config_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = RolloverGroup(name="A / Side", members=[kb("key_a"), side()])
    window, keypad, mouse = _two_device_window(monkeypatch, [group])
    edited: list[str] = []
    monkeypatch.setattr(keypad, "_show_function_editor", lambda button: edited.append(button.id))

    info = mouse._describe_rollover_member(kb("key_a"))
    assert (info.label, info.device) == ("A", "Keypad")
    assert "Left Stick X" in info.mapping
    unknown = mouse._describe_rollover_member(RolloverMember("ffff:0000", "key_q"))
    assert (unknown.label, unknown.device, unknown.mapping) == (
        "key_q",
        "ffff:0000",
        "No mapping in this profile",
    )

    # Editing a member's mapping switches to its device tab first.
    mouse._edit_rollover_member_mapping(kb("key_a"))
    assert edited == ["key_a"]
    assert window.tab_view.selected is window._device_pages[HARDWARE_ID]

    # Change Members from the editor preselects members on every device.
    mouse._activate_mapping_button(mouse._button_by_id("btn_side"), False)
    dialog = window.rollover_state.dialog
    assert dialog is not None
    dialog._on_change_members_clicked(None)
    assert keypad._rollover_selection.selected == [kb("key_a"), side()]
    assert keypad.rollover_finish_button.get_label() == "Save"
    assert keypad._button_widgets["key_a"].has_css_class("rollover-selected")
