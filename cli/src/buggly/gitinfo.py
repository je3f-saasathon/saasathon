"""The GitHub repo and commit of the working directory, from git."""

import re
import subprocess

# https://github.com/owner/name(.git), git@github.com:owner/name(.git), ssh://git@github.com/owner/name
_GITHUB = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def parse_remote(url: str) -> str | None:
    """owner/name from a GitHub remote URL, else None."""
    match = _GITHUB.search(url.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


def _git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def github_repo() -> str | None:
    """owner/name of origin, else of the first GitHub remote."""
    origin = parse_remote(_git("remote", "get-url", "origin"))
    if origin:
        return origin
    for name in _git("remote").split():
        repo = parse_remote(_git("remote", "get-url", name))
        if repo:
            return repo
    return None


def commit() -> str:
    return _git("rev-parse", "--short", "HEAD")
