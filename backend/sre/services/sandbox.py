import io
import os
import posixpath
import tarfile
import time
from pathlib import Path

from django.conf import settings

WORKSPACE = "/workspace"
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

    def __init__(self, work_tree: Path, name: str):
        self.work_tree = work_tree
        self.name = name
        self.container = None

    def __enter__(self):
        import docker

        try:
            client = docker.from_env()
            self.container = client.containers.run(
                settings.SRE_SANDBOX_IMAGE,
                command=["sleep", "infinity"],
                name=self.name,
                detach=True,
                auto_remove=False,
                environment={},
                network_mode=settings.SRE_SANDBOX_NETWORK,
                volumes={str(self.work_tree): {"bind": WORKSPACE, "mode": "rw"}},
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

    def _path(self, path: str) -> str:
        joined = posixpath.normpath(posixpath.join(WORKSPACE, path.lstrip("/")))
        if joined != WORKSPACE and not joined.startswith(WORKSPACE + "/"):
            raise SandboxError(f"path escapes the repo: {path}")
        return joined

    def run(self, command: str) -> tuple[int, str]:
        exit_code, output = self.container.exec_run(
            ["timeout", str(COMMAND_TIMEOUT_SECONDS), "sh", "-c", command],
            workdir=WORKSPACE,
            demux=False,
        )
        return exit_code, _truncate((output or b"").decode(errors="replace"))

    def read_file(self, path: str) -> str:
        exit_code, output = self.container.exec_run(["cat", "--", self._path(path)])
        text = (output or b"").decode(errors="replace")
        if exit_code != 0:
            raise SandboxError(text.strip() or f"cannot read {path}")
        return _truncate(text)

    def list_files(self, path: str = ".") -> str:
        target = self._path(path)
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
