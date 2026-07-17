import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from keymasq.keymasqd.macro_file import (
    MacroFileMeta,
    MacroFileSnapshot,
    load_macro,
    macro_payload_from_events,
    write_macro,
)


def _open_fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_write_macro_temp_file_is_private_before_events_are_written(tmp_path: Path) -> None:
    path = tmp_path / "macro.kmacro.xz"
    old_tmp_pathname = path.with_suffix(path.suffix + ".tmp")
    observed_modes: list[int] = []
    observed_tmp_names: list[str] = []

    def events() -> Iterator[dict[str, object]]:
        tmp_files = list(tmp_path.glob(f".{path.name}.*.tmp"))
        assert len(tmp_files) == 1
        observed_tmp_names.append(tmp_files[0].name)
        observed_modes.append(stat.S_IMODE(tmp_files[0].stat().st_mode))
        yield {"type": 1, "code": 30, "value": 1, "t_us": 0}

    old_umask = os.umask(0)
    try:
        write_macro(path, MacroFileMeta(name="macro", event_count=1), events())
    finally:
        os.umask(old_umask)

    assert observed_modes == [0o600]
    assert observed_tmp_names
    assert not old_tmp_pathname.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_macro_closes_temp_file_descriptor(tmp_path: Path) -> None:
    baseline = _open_fd_count()

    for index in range(10):
        write_macro(
            tmp_path / f"macro-{index}.kmacro.xz",
            MacroFileMeta(name=f"macro-{index}", event_count=1),
            [{"type": 1, "code": 30, "value": 1, "t_us": index}],
        )
        assert _open_fd_count() == baseline


def test_macro_snapshot_close_does_not_block_suspended_stream(tmp_path: Path) -> None:
    path = tmp_path / "macro.kmacro.xz"
    events = [{"type": 1, "code": index, "value": 1, "t_us": index} for index in range(200)]
    write_macro(path, MacroFileMeta(name="macro", event_count=len(events)), events)
    snapshot = MacroFileSnapshot(path)
    iterator = snapshot.iter_events()

    assert next(iterator) == events[0]
    snapshot.close()

    assert list(iterator) == events[1:]


def test_write_macro_without_overwrite_preserves_existing_destination(tmp_path: Path) -> None:
    path = tmp_path / "macro.kmacro.xz"
    original_event = {"type": 1, "code": 30, "value": 1, "t_us": 0}
    replacement_event = {"type": 1, "code": 31, "value": 1, "t_us": 100}

    write_macro(path, MacroFileMeta(name="macro", event_count=1), [original_event])

    with pytest.raises(FileExistsError):
        write_macro(
            path,
            MacroFileMeta(name="macro", event_count=1),
            [replacement_event],
            overwrite=False,
        )

    assert load_macro(path)["events"] == [original_event]


def test_macro_payload_device_types_skip_missing_and_macro_events() -> None:
    payload = macro_payload_from_events(
        {"name": "macro"},
        [
            {"t_us": 0},
            {"device_type": "", "t_us": 1},
            {"device_type": "macro", "t_us": 2},
            {"device_type": "keyboard", "t_us": 3},
            {"macro_action": "mouse_move_natural_abs", "t_us": 4},
        ],
    )

    assert payload["device_types"] == ["keyboard", "mouse"]


def test_macro_meta_preserves_legacy_move_to_start_presence() -> None:
    current = MacroFileMeta.from_payload({"name": "current"}).to_payload()
    legacy = MacroFileMeta.from_payload(
        {
            "name": "legacy",
            "move_to_start": False,
            "start_x": 10,
            "start_y": 20,
        }
    ).to_payload()

    assert "move_to_start" not in current
    assert "start_x" not in current
    assert "start_y" not in current
    assert legacy["move_to_start"] is False
    assert legacy["start_x"] == 10
    assert legacy["start_y"] == 20
