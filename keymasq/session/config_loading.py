import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import cast


@dataclass(frozen=True)
class ConfigLoadFailure:
    path: Path
    message: str


class ConfigLoadError(RuntimeError):
    def __init__(self, config_kind: str, failures: list[ConfigLoadFailure]) -> None:
        self.config_kind = config_kind
        self.failures = tuple(failures)
        details = "; ".join(f"{failure.path}: {failure.message}" for failure in failures)
        super().__init__(f"Failed to load {config_kind} config: {details}")


def _config_files(config_dir: Path, *, sort_paths: bool) -> list[Path]:
    if not config_dir.exists():
        return []
    files = list(config_dir.glob("*.toml"))
    if sort_paths:
        files.sort()
    return files


async def load_config_files[ConfigT](
    config_dir: Path,
    *,
    config_kind: str,
    strict: bool,
    load_config: Callable[[Path], ConfigT | Awaitable[ConfigT]],
    logger: logging.Logger,
    failure_log_message: str = "Failed to load %s: %s",
    sort_paths: bool = False,
) -> list[tuple[Path, ConfigT]]:
    loaded_configs: list[tuple[Path, ConfigT]] = []
    failures: list[ConfigLoadFailure] = []

    for config_file in await asyncio.to_thread(_config_files, config_dir, sort_paths=sort_paths):
        try:
            loaded_config = await asyncio.to_thread(load_config, config_file)
            if inspect.isawaitable(loaded_config):
                loaded_config = await cast(Awaitable[ConfigT], loaded_config)
            loaded_configs.append((config_file, cast(ConfigT, loaded_config)))
        except Exception as exc:
            logger.error(failure_log_message, config_file, exc)
            failures.append(ConfigLoadFailure(config_file, str(exc)))

    if strict and failures:
        raise ConfigLoadError(config_kind, failures)

    return loaded_configs


def load_config_files_sync[ConfigT](
    config_dir: Path,
    *,
    config_kind: str,
    strict: bool,
    load_config: Callable[[Path], ConfigT],
    logger: logging.Logger,
    failure_log_message: str = "Failed to load %s: %s",
    sort_paths: bool = False,
) -> list[tuple[Path, ConfigT]]:
    async def _load() -> list[tuple[Path, ConfigT]]:
        return await load_config_files(
            config_dir,
            config_kind=config_kind,
            strict=strict,
            load_config=load_config,
            logger=logger,
            failure_log_message=failure_log_message,
            sort_paths=sort_paths,
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_load())

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(_load())).result()
