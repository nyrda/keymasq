import os
from pathlib import Path
from typing import BinaryIO

import pytest

from keymasq.common.config_files import write_config_atomically


def test_write_config_atomically_fsyncs_file_then_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_path = tmp_path / "config.toml"
    events: list[str] = []
    directory_fd = 999_999
    original_close = os.close
    original_open = os.open
    original_replace = os.replace

    def fake_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if os.fspath(path) == os.fspath(tmp_path):
            events.append("open_dir")
            assert flags & os.O_DIRECTORY
            return directory_fd
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def fake_fsync(fd: int) -> None:
        if fd == directory_fd:
            events.append("fsync_dir")
        else:
            events.append("fsync_file")

    def fake_close(fd: int) -> None:
        if fd == directory_fd:
            events.append("close_dir")
        else:
            original_close(fd)

    def fake_replace(
        src: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        dst: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> None:
        events.append("replace")
        original_replace(src, dst)

    def write_config(config_file: BinaryIO) -> None:
        config_file.write(b"name = \"Test\"\n")

    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(os, "fsync", fake_fsync)
    monkeypatch.setattr(os, "close", fake_close)
    monkeypatch.setattr(os, "replace", fake_replace)

    write_config_atomically(target_path, write_config)

    assert target_path.read_bytes() == b"name = \"Test\"\n"
    assert events == ["fsync_file", "replace", "open_dir", "fsync_dir", "close_dir"]


def test_write_config_atomically_propagates_parent_fsync_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_path = tmp_path / "config.toml"
    directory_fd = 999_999
    events: list[str] = []
    original_close = os.close
    original_open = os.open

    def fake_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if os.fspath(path) == os.fspath(tmp_path):
            return directory_fd
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def fake_fsync(fd: int) -> None:
        if fd == directory_fd:
            events.append("fsync_dir")
            raise OSError("directory fsync failed")

    def fake_close(fd: int) -> None:
        if fd == directory_fd:
            events.append("close_dir")
        else:
            original_close(fd)

    def write_config(config_file: BinaryIO) -> None:
        config_file.write(b"name = \"Test\"\n")

    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(os, "fsync", fake_fsync)
    monkeypatch.setattr(os, "close", fake_close)

    with pytest.raises(OSError, match="directory fsync failed"):
        write_config_atomically(target_path, write_config)

    assert target_path.read_bytes() == b"name = \"Test\"\n"
    assert events == ["fsync_dir", "close_dir"]


def test_write_config_atomically_propagates_parent_open_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_path = tmp_path / "config.toml"
    original_open = os.open

    def fake_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if os.fspath(path) == os.fspath(tmp_path):
            raise OSError("directory open failed")
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def write_config(config_file: BinaryIO) -> None:
        config_file.write(b"name = \"Test\"\n")

    monkeypatch.setattr(os, "open", fake_open)

    with pytest.raises(OSError, match="directory open failed"):
        write_config_atomically(target_path, write_config)

    assert target_path.read_bytes() == b"name = \"Test\"\n"


def test_write_config_atomically_exclusive_create_preserves_existing_file(
    tmp_path: Path,
) -> None:
    target_path = tmp_path / "config.toml"
    target_path.write_bytes(b'name = "Existing"\n')

    def write_config(config_file: BinaryIO) -> None:
        config_file.write(b'name = "Replacement"\n')

    with pytest.raises(FileExistsError):
        write_config_atomically(target_path, write_config, overwrite=False)

    assert target_path.read_bytes() == b'name = "Existing"\n'
    assert list(tmp_path.glob(f".{target_path.name}.*")) == []


def test_write_config_atomically_exclusive_create_cleans_temp_on_write_failure(
    tmp_path: Path,
) -> None:
    target_path = tmp_path / "config.toml"

    def write_config(config_file: BinaryIO) -> None:
        config_file.write(b'name = "Partial"\n')
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        write_config_atomically(target_path, write_config, overwrite=False)

    assert not target_path.exists()
    assert list(tmp_path.glob(f".{target_path.name}.*")) == []
