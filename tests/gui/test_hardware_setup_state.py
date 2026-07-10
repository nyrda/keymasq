from keymasq.gui.wizards.hardware_setup.state import (
    DiscoverySelection,
    TemplateSelection,
    WizardNavigation,
)


def test_navigation_blocks_until_discovery_and_routes_selected_template() -> None:
    navigation = WizardNavigation()

    assert (
        navigation.next_action(
            has_selection=False,
            discovery_inflight=False,
            configure_mode="mouse",
        )
        == "none"
    )
    assert (
        navigation.next_action(
            has_selection=True,
            discovery_inflight=True,
            configure_mode="mouse",
        )
        == "none"
    )
    assert (
        navigation.next_action(
            has_selection=True,
            discovery_inflight=False,
            configure_mode="gamepad",
        )
        == "show_describe"
    )
    assert (
        navigation.next_action(
            has_selection=True,
            discovery_inflight=False,
            configure_mode="gamepad",
        )
        == "save_gamepad"
    )
    assert navigation.back() is True
    assert navigation.page == "select"


def test_navigation_emits_raw_selection_without_entering_template_page() -> None:
    navigation = WizardNavigation(select_evdev_only=True)

    assert (
        navigation.next_action(
            has_selection=True,
            discovery_inflight=False,
            configure_mode="",
        )
        == "emit_evdev"
    )
    assert navigation.page == "select"


def test_discovery_selection_rejects_stale_generation() -> None:
    state = DiscoverySelection()
    first = {"hardware_id": "1234:0001"}
    second = {"hardware_id": "1234:0002"}

    first_request = state.select(first)
    second_request = state.select(second)

    assert state.accepts(first_request, "1234:0001", "1234:0002") is False
    assert state.accepts(second_request, "1234:0002", "1234:0002") is True
    state.finish_discovery(first_request)
    assert state.discovering is True
    state.finish_discovery(second_request)
    assert state.discovering is False

    state.clear_selection()
    assert state.selected_device is None
    assert state.request_id > second_request


def test_template_selection_orders_modes_and_preserves_valid_choice() -> None:
    state = TemplateSelection(current="keyboard")

    assert state.refresh({"mouse", "keyboard", "gamepad"}, show_raw=False) == [
        "gamepad",
        "mouse_keyboard",
        "mouse",
        "keyboard",
    ]
    assert state.current == "keyboard"
    assert state.refresh(set(), show_raw=True) == ["custom"]
    assert state.current == "custom"
