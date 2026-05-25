from dataclasses import dataclass
from pathlib import Path


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
