from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


def load_env_file(path: str | Path = DEFAULT_ENV_PATH, *, override: bool = False) -> dict[str, str]:
    env_path = Path(path)
    if not env_path.exists():
        return {}

    loaded: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = _unquote(value.strip())
        if override or key not in os.environ:
            os.environ[key] = value
        loaded[key] = os.environ.get(key, "")
    return loaded


def load_project_env() -> dict[str, str]:
    return load_env_file(DEFAULT_ENV_PATH)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
