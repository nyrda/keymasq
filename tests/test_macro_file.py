import os
import stat
from collections.abc import Iterator
from pathlib import Path

from keymasq.keymasqd.macro_file import MacroFileMeta, write_macro


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
