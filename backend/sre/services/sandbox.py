import io
import os
import posixpath
import tarfile
import time
from pathlib import Path

from django.conf import settings

WORKSPACE = "/workspace"
# Read-only copies of neighbouring services' repos (service mesh), one directory each.
NEIGHBOURS = "/neighbours"
COMMAND_TIMEOUT_SECONDS = 300
MAX_OUTPUT_CHARS = 8000
LIST_SOURCE_FILES = (
    "find . -type f -size -100k -not -path './.git/*' -not -path './.venv/*' "
    "-not -path '*/node_modules/*' -not -path '*/__pycache__/*' -not -path './dist/*' "
    "-not -path './build/*' | head -5000"
)


class SandboxError(Exception):
    pass


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return text[:half] + "\n...[truncated]...\n" + text[-half:]


class Sandbox:
    """A throwaway container holding only the repo work tree: no env vars, no secrets,
    no network unless SRE_SANDBOX_NETWORK says otherwise. Every agent tool call
    (file reads/writes and commands) happens in here, never on the worker host."""

    def __init__(self, work_tree: Path, name: str, network: str | None = None,
                 neighbours: dict[str, Path] | None = None, read_only: bool = False):
        self.work_tree = work_tree
        # A scan (remediation agents) only reads: the repo is mounted read-only too.
        self.workspace_mode = "ro" if read_only else "rw"
        # {directory name under /neighbours: host path}, mounted read-only.
        self.neighbours = neighbours or {}
        self.name = name
        # The network the container starts on; isolate() can take it off later.
        self.network = network or settings.SRE_SANDBOX_NETWORK
        self.container = None
        self.client = None
        # Set by isolate(): tells package managers not to try the network. Not secrets.
        self.command_env: dict[str, str] = {}

    def __enter__(self):
        import docker

        try:
            self.client = client = docker.from_env()
            self.container = client.containers.run(
                settings.SRE_SANDBOX_IMAGE,
                command=["sleep", "infinity"],
                name=self.name,
                detach=True,
                auto_remove=False,
                environment={},
                network_mode=self.network,
                volumes={
                    str(self.work_tree): {"bind": WORKSPACE, "mode": self.workspace_mode},
                    **{str(path): {"bind": f"{NEIGHBOURS}/{name}", "mode": "ro"}
                       for name, path in self.neighbours.items()},
                },
                working_dir=WORKSPACE,
                # Same uid as the worker so it can commit and clean up what the agent wrote.
                user=f"{os.getuid()}:{os.getgid()}",
                mem_limit="2g",
                nano_cpus=2_000_000_000,
                pids_limit=512,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                read_only=False,
            )
        except docker.errors.DockerException as exc:
            raise SandboxError(f"could not start sandbox: {exc}") from exc
        return self

    def __exit__(self, *exc):
        if self.container is not None:
            try:
                self.container.remove(force=True)
            except Exception:
                pass

    def _path(self, path: str, roots: tuple[str, ...] = (WORKSPACE,)) -> str:
        """An absolute path inside one of `roots` is taken as is (only /workspace for
        writes); anything else is relative to /workspace and must stay inside it."""
        if path.startswith("/"):
            absolute = posixpath.normpath(path)
            if any(absolute == root or absolute.startswith(root + "/") for root in roots):
                return absolute
        joined = posixpath.normpath(posixpath.join(WORKSPACE, path.lstrip("/")))
        if joined != WORKSPACE and not joined.startswith(WORKSPACE + "/"):
            raise SandboxError(f"path escapes the repo: {path}")
        return joined

    def _read_path(self, path: str) -> str:
        return self._path(path, (WORKSPACE, NEIGHBOURS) if self.neighbours else (WORKSPACE,))

    def isolate(self) -> None:
        """Disconnect every network, then prove there's no way out. Raises (and the attempt
        fails) rather than letting the agent run with network it shouldn't have."""
        import docker

        try:
            self.container.reload()
            for name in list(self.container.attrs["NetworkSettings"]["Networks"]):
                self.client.networks.get(name).disconnect(self.container, force=True)
            self.container.reload()
        except docker.errors.DockerException as exc:
            raise SandboxError(f"could not disconnect the sandbox from the network: {exc}") from exc
        if self.container.attrs["NetworkSettings"]["Networks"]:
            raise SandboxError("sandbox still has a network after disconnecting")
        exit_code, _ = self.container.exec_run(
            ["python3", "-c", "import socket; socket.create_connection(('1.1.1.1', 53), 3)"]
        )
        if exit_code == 0:
            raise SandboxError("sandbox can still reach the internet after disconnecting")
        self.command_env = {"UV_OFFLINE": "1", "PIP_NO_INDEX": "1"}

    def run(self, command: str, timeout: int = COMMAND_TIMEOUT_SECONDS) -> tuple[int, str]:
        exit_code, output = self.container.exec_run(
            ["timeout", str(timeout), "sh", "-c", command],
            workdir=WORKSPACE,
            environment=self.command_env,
            demux=False,
        )
        return exit_code, _truncate((output or b"").decode(errors="replace"))

    def _cat(self, target: str, path: str) -> str:
        exit_code, output = self.container.exec_run(["cat", "--", target])
        text = (output or b"").decode(errors="replace")
        if exit_code != 0:
            raise SandboxError(text.strip() or f"cannot read {path}")
        return text

    def read_file(self, path: str, start: int | None = None, end: int | None = None) -> str:
        """The whole file, or lines start..end (1-based, inclusive) of it."""
        text = self._cat(self._read_path(path), path)
        if start is None and end is None:
            return _truncate(text)
        lines = text.splitlines(keepends=True)
        first = max(1, int(start or 1))
        last = min(len(lines), int(end or len(lines)))
        return (f"[lines {first}-{last} of {len(lines)}]\n"
                + _truncate("".join(lines[first - 1:last])))

    def read_text(self, path: str) -> str:
        """The whole file, untruncated: for the worker's own use (e.g. parsing), never
        shown to the agent as is."""
        return self._cat(self._read_path(path), path)

    def source_files(self) -> list[str]:
        """Every regular file in the repo, minus dependency and build directories,
        untruncated (the agent's list_files caps its output)."""
        exit_code, output = self.container.exec_run(["sh", "-c", LIST_SOURCE_FILES],
                                                    workdir=WORKSPACE)
        if exit_code != 0:
            return []
        return [line.removeprefix("./") for line in
                (output or b"").decode(errors="replace").splitlines() if line.strip()]

    def replace_in_file(self, path: str, old: str, new: str) -> None:
        """Replaces the one occurrence of `old`; refuses when it's missing or ambiguous,
        so an edit can never land somewhere the agent didn't mean."""
        if not old:
            raise SandboxError("old text is empty; use write_file to create a file")
        text = self._cat(self._path(path), path)
        count = text.count(old)
        if count != 1:
            raise SandboxError(
                f"old text found {count} times in {path}; it must match exactly once "
                "(copy it exactly, with a few surrounding lines)")
        self.write_file(path, text.replace(old, new, 1))

    def list_files(self, path: str = ".") -> str:
        target = self._read_path(path)
        exit_code, output = self.container.exec_run(
            ["sh", "-c", 'find "$1" -maxdepth 3 -not -path "*/node_modules/*" | head -500', "_", target]
        )
        return _truncate((output or b"").decode(errors="replace"))

    def write_file(self, path: str, content: str) -> None:
        target = self._path(path)
        data = content.encode()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            info = tarfile.TarInfo(name=posixpath.basename(target))
            info.size = len(data)
            info.mode = 0o644
            # Docker extracts as root and keeps tar ownership; stamp the worker's uid.
            info.uid, info.gid = os.getuid(), os.getgid()
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
        parent = posixpath.dirname(target)
        self.container.exec_run(["mkdir", "-p", parent])
        if not self.container.put_archive(parent, buffer.getvalue()):
            raise SandboxError(f"cannot write {path}")
