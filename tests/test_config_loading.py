import logging
import tomllib
from pathlib import Path
from typing import cast

import pytest

from keymasq.session.config_loading import ConfigLoadError, load_config_files


def _load_name(path: Path) -> str:
    with path.open("rb") as config_file:
        data = cast(dict[str, object], tomllib.load(config_file))
    return str(data["name"])


@pytest.mark.asyncio
async def test_load_config_files_collects_failures_in_non_strict_mode(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    valid_path = config_dir / "valid.toml"
    broken_path = config_dir / "broken.toml"
    valid_path.write_text('name = "Working"\n', encoding="utf-8")
    broken_path.write_text("name = [invalid\n", encoding="utf-8")
    logger = logging.getLogger("tests.config_loading")

    with caplog.at_level(logging.ERROR, logger=logger.name):
        loaded = await load_config_files(
            config_dir,
            config_kind="test",
            strict=False,
            load_config=_load_name,
            logger=logger,
            sort_paths=True,
        )

    assert loaded == [(valid_path, "Working")]
    assert f"Failed to load {broken_path}" in caplog.text


@pytest.mark.asyncio
async def test_load_config_files_raises_collected_failures_in_strict_mode(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    valid_path = config_dir / "valid.toml"
    broken_path = config_dir / "broken.toml"
    valid_path.write_text('name = "Working"\n', encoding="utf-8")
    broken_path.write_text("name = [invalid\n", encoding="utf-8")

    with pytest.raises(ConfigLoadError) as exc_info:
        await load_config_files(
            config_dir,
            config_kind="test",
            strict=True,
            load_config=_load_name,
            logger=logging.getLogger("tests.config_loading"),
            sort_paths=True,
        )

    error = exc_info.value
    assert error.config_kind == "test"
    assert len(error.failures) == 1
    assert error.failures[0].path == broken_path


@pytest.mark.asyncio
async def test_load_config_files_accepts_async_loader(tmp_path: Path) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    valid_path = config_dir / "valid.toml"
    valid_path.write_text('name = "Working"\n', encoding="utf-8")

    async def _load_name_async(path: Path) -> str:
        return _load_name(path)

    loaded = await load_config_files(
        config_dir,
        config_kind="test",
        strict=True,
        load_config=_load_name_async,
        logger=logging.getLogger("tests.config_loading"),
        sort_paths=True,
    )

    assert loaded == [(valid_path, "Working")]
