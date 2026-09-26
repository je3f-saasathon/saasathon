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
                 neighbours: dict[str, Path] | None = None):
        self.work_tree = work_tree
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
                    str(self.work_tree): {"bind": WORKSPACE, "mode": "rw"},
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

    def read_file(self, path: str) -> str:
        exit_code, output = self.container.exec_run(["cat", "--", self._read_path(path)])
        text = (output or b"").decode(errors="replace")
        if exit_code != 0:
            raise SandboxError(text.strip() or f"cannot read {path}")
        return _truncate(text)

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
