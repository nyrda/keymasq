from tests.gui.macro_editor_dialog_support import _build_macro_dialog, _FakeSlurpCapture


def test_macro_editor_support_imports_shared_helpers() -> None:
    fake_slurp = _FakeSlurpCapture(available=True)
    captured: list[int] = []
    callback = captured.append

    fake_slurp.set_compositor("hyprland")
    fake_slurp.capture_point(callback)

    assert fake_slurp.available is True
    assert fake_slurp.compositor == "hyprland"
    assert fake_slurp.capture_callback is callback
    assert callable(_build_macro_dialog)
