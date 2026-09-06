import pytest

gi = pytest.importorskip("gi")


@pytest.fixture
def dialog_module(monkeypatch, temp_config_dir):
    from keymasq.gui.widgets import virtual_devices_dialog

    monkeypatch.setattr(
        virtual_devices_dialog, "session_request_async", lambda *args, **kwargs: None
    )
    return virtual_devices_dialog


def test_template_form_edits_ranges_and_retains_invalid_draft(dialog_module) -> None:
    from keymasq.common.virtual_device_templates import LOGITECH_EXTREME_3D_TEMPLATE

    saved = []
    dialog = dialog_module.VirtualTemplateEditorDialog(
        dialog_module.unique_template_copy(LOGITECH_EXTREME_3D_TEMPLATE, ()),
        lambda template: saved.append(template) or True,
    )
    axis = dialog._axis_rows[0]
    axis.range_rows["maximum"].set_value(65535)
    axis.range_rows["rest"].set_value(32767)
    dialog._save(None)
    assert saved[0].axes[0].maximum == 65535
    assert saved[0].axes[0].rest == 32767
    assert saved[0].buttons == LOGITECH_EXTREME_3D_TEMPLATE.buttons

    axis.range_rows["rest"].set_value(70000)
    dialog._save(None)
    assert len(saved) == 1
    assert "inside the axis range" in dialog._status.get_text()
    assert axis.range_rows["rest"].get_value() == 70000


def test_duplicate_twice_keeps_both_customizations(dialog_module, monkeypatch) -> None:
    from keymasq.common.virtual_device_templates import XBOX_360_TEMPLATE

    editors = []
    monkeypatch.setattr(
        dialog_module.VirtualTemplateEditorDialog,
        "present",
        lambda self, parent: editors.append(self),
    )
    dialog = dialog_module.VirtualDevicesDialog()
    for label in ("First layout", "Second layout"):
        dialog._duplicate_template(None, XBOX_360_TEMPLATE)
        editor = editors[-1]
        assert editor._id_row.get_editable()
        editor._label_row.set_text(label)
        editor._save(None)

    assert [template.id for template in dialog._config.templates] == [
        "xbox-360-copy",
        "xbox-360-copy-2",
    ]
    assert [template.label for template in dialog._config.templates] == [
        "First layout",
        "Second layout",
    ]


def test_duplicate_id_error_stays_in_editor(dialog_module, monkeypatch) -> None:
    from keymasq.common.virtual_device_templates import XBOX_360_TEMPLATE

    editors = []
    closed = []
    monkeypatch.setattr(
        dialog_module.VirtualTemplateEditorDialog,
        "present",
        lambda self, parent: editors.append(self),
    )
    monkeypatch.setattr(
        dialog_module.VirtualTemplateEditorDialog, "close", lambda self: closed.append(self)
    )
    dialog = dialog_module.VirtualDevicesDialog()
    dialog._duplicate_template(None, XBOX_360_TEMPLATE)
    editors[-1]._save(None)
    dialog._duplicate_template(None, XBOX_360_TEMPLATE)
    editors[-1]._id_row.set_text("xbox-360-copy")
    editors[-1]._save(None)
    assert len(closed) == 1
    assert len(dialog._config.templates) == 1
    assert "already in use" in editors[-1]._status.get_text()


def test_add_output_from_template_preselects_and_uses_unique_id(dialog_module, monkeypatch) -> None:
    from keymasq.common.virtual_device_templates import LOGITECH_EXTREME_3D_TEMPLATE

    editors = []
    monkeypatch.setattr(
        dialog_module.VirtualDeviceInstanceDialog,
        "present",
        lambda self, parent: editors.append(self),
    )
    dialog = dialog_module.VirtualDevicesDialog()
    for _ in range(2):
        dialog._use_template(None, LOGITECH_EXTREME_3D_TEMPLATE)
        editors[-1]._save(None)
    assert [device.template_id for device in dialog._config.devices] == [
        LOGITECH_EXTREME_3D_TEMPLATE.id
    ] * 2
    assert len({device.output_id for device in dialog._config.devices}) == 2
    assert dialog._dirty
    assert dialog._apply_button.get_sensitive()
    assert not dialog._empty_outputs.get_visible()


def test_apply_freezes_draft_and_restores_editor_on_failure(dialog_module, monkeypatch) -> None:
    requests = []
    monkeypatch.setattr(
        dialog_module,
        "session_request_async",
        lambda payload, callback, **kwargs: requests.append((payload, callback)),
    )
    dialog = dialog_module.VirtualDevicesDialog()
    dialog._set_config(devices=[])
    dialog._apply(None)
    assert not dialog._content.get_sensitive()
    assert not dialog._apply_button.get_sensitive()
    requests[-1][1]({"status": "error", "message": "Could not create output"})
    assert dialog._content.get_sensitive()
    assert dialog._apply_button.get_sensitive()
    assert dialog._dirty
    assert dialog._status.get_text() == "Could not create output"


def test_custom_template_editor_preserves_layout_and_adds_batch(dialog_module):
    from keymasq.common.virtual_device_templates import LOGITECH_EXTREME_3D_TEMPLATE

    template = dialog_module.unique_template_copy(LOGITECH_EXTREME_3D_TEMPLATE, ())
    saved = []
    dialog = dialog_module.VirtualTemplateEditorDialog(
        template, lambda value: saved.append(value) or True, creating=True
    )
    assert dialog._layout_row.get_selected() == 1
    dialog._append_numbered_buttons(20)
    dialog._save(None)
    assert saved[0].layout == "flight-stick"
    assert len(saved[0].buttons) == 32
    assert [button.evdev for button in saved[0].buttons[12:]] == [
        f"btn_trigger_happy{i}" for i in range(1, 21)
    ]
    assert saved[0].buttons[:12] == LOGITECH_EXTREME_3D_TEMPLATE.buttons
