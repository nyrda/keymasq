import contextlib
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, cast


def write_config_atomically(path: Path, write: Callable[[BinaryIO], None]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path: Path | None = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as config_file:
            binary_file = cast(BinaryIO, config_file)
            write(binary_file)
            binary_file.flush()
            os.fsync(binary_file.fileno())
        os.replace(temp_path, path)
        temp_path = None
        with contextlib.suppress(OSError):
            dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        if temp_path is not None:
            with contextlib.suppress(FileNotFoundError):
                temp_path.unlink()
