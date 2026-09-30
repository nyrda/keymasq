# pyright: reportUnusedFunction=false

from __future__ import annotations

from . import _runtime


def _on_recording_stopped(window, event: dict) -> None:
    present_pending_macro_save_dialog(window, event)


def present_pending_macro_save_dialog(window, recording_data: dict | None = None) -> bool:
    from keymasq.gui.widgets.save_macro_dialog import SaveMacroDialog

    if window._save_macro_dialog is not None:
        window._save_macro_dialog.present(window)
        return True

    if recording_data is None:
        return False

    dialog = SaveMacroDialog(window, recording_data)

    def on_save_macro_dialog_closed(dialog) -> None:
        _on_save_macro_dialog_closed(window, dialog)

    dialog.connect("closed", on_save_macro_dialog_closed)
    window._save_macro_dialog = dialog
    dialog.present(window)
    return True


def _on_save_macro_dialog_closed(window, dialog) -> None:
    if dialog is window._save_macro_dialog:
        window._save_macro_dialog = None


def _close_dialogs_for_recording_start(window) -> None:
    for dialog in (window._record_macro_dialog, window._macro_manager_dialog):
        if dialog is not None:
            dialog.close()


def set_macro_manager_dialog(window, dialog: _runtime.Adw.Dialog | None) -> None:
    window._macro_manager_dialog = dialog


def _update_recording_policy(window, status_data: dict | None) -> bool:
    if not isinstance(status_data, dict) or status_data.get("status") != "ok":
        return False
    allowed = bool(status_data.get("macro_recording_allowed", True))
    changed = allowed != window._macro_recording_allowed
    window._macro_recording_allowed = allowed
    window._emergency_cancel_combo_enabled = bool(
        status_data.get("emergency_cancel_combo_enabled", True)
    )
    return changed


def macro_recording_allowed(window) -> bool:
    return bool(window._macro_recording_allowed)


def emergency_cancel_combo_enabled(window) -> bool:
    return bool(window._emergency_cancel_combo_enabled)


def present_recording_settings_dialog(window) -> None:
    if window._record_macro_dialog is not None:
        window._record_macro_dialog.present(window)
        return

    from keymasq.gui.widgets.record_macro_dialog import RecordMacroDialog

    dialog = RecordMacroDialog(window)

    def on_record_macro_dialog_closed(dialog) -> None:
        _on_record_macro_dialog_closed(window, dialog)

    dialog.connect("closed", on_record_macro_dialog_closed)
    window._record_macro_dialog = dialog
    dialog.present(window)


def _on_record_macro_dialog_closed(window, dialog) -> None:
    if dialog is window._record_macro_dialog:
        window._record_macro_dialog = None
