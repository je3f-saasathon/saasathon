"""Where the CLI keeps its API URL and token: ~/.config/buggly/config.json (0600)."""

import json
import os
from pathlib import Path

DEFAULT_API_URL = "https://api.buggly.dev"


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "buggly" / "config.json"


def load() -> dict:
    try:
        return json.loads(config_path().read_text())
    except (OSError, ValueError):
        return {}


def save(data: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    # Created 0600 so the token is never readable by others, even briefly.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def api_url(override: str | None = None) -> str:
    """--api-url, then BUGGLY_API_URL, then the saved one, then production."""
    url = override or os.environ.get("BUGGLY_API_URL") or load().get("api_url") or DEFAULT_API_URL
    return url.rstrip("/")


def token() -> str:
    return os.environ.get("BUGGLY_TOKEN") or load().get("token", "")
