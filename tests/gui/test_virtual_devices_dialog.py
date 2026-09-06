import pytest

gi = pytest.importorskip("gi")


def test_axis_choices_exclude_sentinels():
    from keymasq.gui.widgets.virtual_template_controls import event_names

    axes = event_names(axis=True)
    assert "abs_x" in axes
    assert "abs_throttle" in axes
    assert "abs_max" not in axes
    assert "abs_cnt" not in axes
    assert "btn_trigger_happy1" in event_names(axis=False)


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


@pytest.mark.parametrize("outcome", ["unchanged", "applied", "unavailable", "invalid"])
def test_unknown_apply_outcome_is_reconciled_without_local_save(
    dialog_module, monkeypatch, temp_config_dir, outcome
):
    from keymasq.common.virtual_device_templates import (
        LOGITECH_EXTREME_3D_TEMPLATE_ID,
        VirtualDeviceConfig,
        VirtualDeviceInstance,
        config_to_json,
    )
    from keymasq.session.virtual_devices import save_virtual_device_config

    old = VirtualDeviceConfig()
    save_virtual_device_config(old)
    config_path = temp_config_dir / "virtual_devices.toml"
    previous_bytes = config_path.read_bytes()
    requests = []
    monkeypatch.setattr(
        dialog_module,
        "session_request_async",
        lambda payload, callback, **kwargs: requests.append((payload, callback)),
    )
    dialog = dialog_module.VirtualDevicesDialog()
    dialog._set_config(devices=[VirtualDeviceInstance("flight", LOGITECH_EXTREME_3D_TEMPLATE_ID)])
    candidate = dialog._config
    dialog._apply(None)
    requests[-1][1](None)
    assert requests[-1][0] == {"command": "get_virtual_devices"}
    assert dialog._dirty
    assert dialog._applying
    assert config_path.read_bytes() == previous_bytes
    confirmed = outcome == "applied"
    response = {"status": "ok", "config": config_to_json(candidate if confirmed else old)}
    if outcome == "invalid":
        response["config"] = None
    requests[-1][1](None if outcome == "unavailable" else response)
    assert dialog._dirty is not confirmed
    assert not dialog._applying
    assert dialog._config == candidate
    assert config_path.read_bytes() == previous_bytes
    assert dialog._apply_button.get_sensitive() is not confirmed


def test_apply_timeout_followed_by_rejection_preserves_disk(
    dialog_module, monkeypatch, temp_config_dir, tmp_path
):
    import json
    import socket
    import threading

    from keymasq.common.virtual_device_templates import (
        LOGITECH_EXTREME_3D_TEMPLATE_ID,
        VirtualDeviceConfig,
        VirtualDeviceInstance,
        config_to_json,
    )
    from keymasq.gui import session_client
    from keymasq.session.virtual_devices import save_virtual_device_config

    old = VirtualDeviceConfig()
    save_virtual_device_config(old)
    config_path = temp_config_dir / "virtual_devices.toml"
    original = config_path.read_bytes()
    dialog = dialog_module.VirtualDevicesDialog()
    dialog._set_config(devices=[VirtualDeviceInstance("flight", LOGITECH_EXTREME_3D_TEMPLATE_ID)])
    socket_path = tmp_path / "session.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(2)
    server.settimeout(2)
    timed_out = threading.Event()
    requests = []
    errors = []

    def serve():
        try:
            connection, _ = server.accept()
            with connection, connection.makefile("rb") as reader:
                requests.append(json.loads(reader.readline()))
                assert timed_out.wait(2)
                try:
                    connection.sendall(b'{"status":"error","message":"rejected"}\n')
                except OSError:
                    pass  # The client closes the first connection when its request times out.
            connection, _ = server.accept()
            with connection, connection.makefile("rb") as reader:
                requests.append(json.loads(reader.readline()))
                response = {"status": "ok", "config": config_to_json(old)}
                connection.sendall((json.dumps(response) + "\n").encode())
        except (OSError, ValueError, AssertionError) as exc:
            errors.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = session_client._PersistentSessionConnection()
    monkeypatch.setattr(session_client, "SESSION_SOCKET_PATH", socket_path)

    def request(payload, callback, **kwargs):
        timeout = 0.05 if payload["command"] == "set_virtual_devices" else 1.0
        response = client.request(payload, timeout=timeout)
        if payload["command"] == "set_virtual_devices":
            assert response is None
            timed_out.set()
        callback(response)

    monkeypatch.setattr(dialog_module, "session_request_async", request)
    try:
        dialog._apply(None)
    finally:
        timed_out.set()
        client.shutdown()
        thread.join(3)
        server.close()
    assert not thread.is_alive()
    assert not errors
    assert [item["command"] for item in requests] == ["set_virtual_devices", "get_virtual_devices"]
    assert config_path.read_bytes() == original
    assert dialog._dirty
    assert dialog._apply_button.get_sensitive()
    assert "Could not confirm" in dialog._status.get_text()


def test_output_editor_rejects_same_device_id(dialog_module, monkeypatch):
    editors = []
    monkeypatch.setattr(
        dialog_module.VirtualDeviceInstanceDialog,
        "present",
        lambda self, parent: editors.append(self),
    )
    dialog = dialog_module.VirtualDevicesDialog()
    dialog._new_device(None)
    editor = editors[-1]
    editor._output_row.set_text("same-device")
    editor._save(None)
    assert not dialog._config.devices
    assert "reserved" in editor._status.get_text()


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
