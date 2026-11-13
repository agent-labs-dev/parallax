"""Settings are read from config/defaults.toml next to this file."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

_DEFAULTS = Path(__file__).parent / "config" / "defaults.toml"


@dataclass(frozen=True)
class Settings:
    queue_name: str
    retry_limit: int
    backoff_s: int


def load(path: Path = _DEFAULTS) -> Settings:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return Settings(data["queue"]["name"], data["retry"]["limit"], data["retry"]["backoff_s"])
