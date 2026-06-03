import contextlib
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, cast

import tomli_w


def fsync_parent_dir(path: Path) -> None:
    with contextlib.suppress(OSError):
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def write_config_atomically(
    path: Path,
    write: Callable[[BinaryIO], None],
    *,
    overwrite: bool = True,
    temp_suffix: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=temp_suffix,
        dir=path.parent,
    )
    temp_path: Path | None = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as config_file:
            binary_file = cast(BinaryIO, config_file)
            write(binary_file)
            binary_file.flush()
            os.fsync(binary_file.fileno())

        if overwrite:
            os.replace(temp_path, path)
        else:
            os.link(temp_path, path)
            temp_path.unlink()
        temp_path = None
        fsync_parent_dir(path)
    finally:
        if temp_path is not None:
            with contextlib.suppress(FileNotFoundError):
                temp_path.unlink()


def write_toml_atomically(
    path: Path,
    data: dict[str, object],
    *,
    overwrite: bool = True,
) -> None:
    def write(config_file: BinaryIO) -> None:
        tomli_w.dump(data, config_file)

    write_config_atomically(path, write, overwrite=overwrite)
