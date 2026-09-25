"""Runs a real sandbox container. Skipped when Docker or the image isn't available;
build the image with `make sandbox-image`."""

import os

import pytest

from sre.services.sandbox import Sandbox, SandboxError


def _sandbox_available(image: str) -> bool:
    try:
        import docker

        docker.from_env().images.get(image)
        return True
    except Exception:
        return False


@pytest.fixture
def sandbox(settings, tmp_path, monkeypatch):
    settings.SRE_SANDBOX_NETWORK = "none"
    if not _sandbox_available(settings.SRE_SANDBOX_IMAGE):
        pytest.skip("Docker or sandbox image not available (run `make sandbox-image`)")
    monkeypatch.setenv("SRE_FAKE_WORKER_SECRET", "hunter2")
    (tmp_path / "app.py").write_text("print('hi')\n")
    with Sandbox(tmp_path, name=f"sre-test-{os.getpid()}-{tmp_path.name}") as box:
        yield box, tmp_path


pytestmark = pytest.mark.sandbox


def test_worker_env_is_not_visible(sandbox):
    box, _ = sandbox
    _, output = box.run("env")
    assert "hunter2" not in output
    assert "SECRET" not in output


def test_no_network_by_default(sandbox):
    box, _ = sandbox
    code, _ = box.run(
        "python -c \"import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)\""
    )
    assert code != 0


def test_file_ops_stay_in_the_repo(sandbox):
    box, work_tree = sandbox
    box.write_file("pkg/new.py", "x = 1\n")
    assert (work_tree / "pkg" / "new.py").read_text() == "x = 1\n"
    assert (work_tree / "pkg" / "new.py").stat().st_uid == os.getuid()
    assert "print('hi')" in box.read_file("app.py")
    with pytest.raises(SandboxError):
        box.write_file("../../etc/cron.d/evil", "x")
    with pytest.raises(SandboxError):
        box.read_file("/etc/passwd/../../../workspace/../etc/shadow")


def test_commands_see_only_the_work_tree(sandbox):
    box, _ = sandbox
    code, output = box.run("ls -a /workspace")
    assert code == 0 and "app.py" in output


def test_install_then_isolate_cuts_all_network(settings, tmp_path):
    """The prod flow: start on a network for the dependency install, then disconnect
    before the agent runs. After isolate() nothing can get out."""
    if not _sandbox_available(settings.SRE_SANDBOX_IMAGE):
        pytest.skip("Docker or sandbox image not available (run `make sandbox-image`)")
    reach = "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3)\""
    with Sandbox(tmp_path, name=f"sre-test-iso-{os.getpid()}", network="bridge") as box:
        assert box.run(reach)[0] == 0  # online for the install step
        box.isolate()
        assert box.run(reach)[0] != 0
        assert box.run("getent hosts pypi.org")[0] != 0
        box.container.reload()
        assert box.container.attrs["NetworkSettings"]["Networks"] == {}
        assert box.run("echo $UV_OFFLINE $PIP_NO_INDEX")[1].strip() == "1 1"
