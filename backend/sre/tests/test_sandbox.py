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


def test_neighbour_repos_are_readable_but_not_writable(settings, tmp_path):
    settings.SRE_SANDBOX_NETWORK = "none"
    if not _sandbox_available(settings.SRE_SANDBOX_IMAGE):
        pytest.skip("Docker or sandbox image not available (run `make sandbox-image`)")
    work_tree, neighbour = tmp_path / "tree", tmp_path / "neighbours" / "acme-worker"
    work_tree.mkdir()
    neighbour.mkdir(parents=True)
    (neighbour / "client.py").write_text("def charge(): ...\n")
    with Sandbox(work_tree, name=f"sre-test-{os.getpid()}-{tmp_path.name}",
                 neighbours={"acme-worker": neighbour}) as box:
        assert "def charge" in box.read_file("/neighbours/acme-worker/client.py")
        assert "client.py" in box.list_files("/neighbours")
        exit_code, _ = box.run("echo x > /neighbours/acme-worker/client.py")
        assert exit_code != 0
        # write_file only ever writes into /workspace.
        box.write_file("/neighbours/acme-worker/client.py", "pwned")
    assert (neighbour / "client.py").read_text() == "def charge(): ...\n"
    assert (work_tree / "neighbours" / "acme-worker" / "client.py").read_text() == "pwned"


def test_edit_replaces_exactly_one_match(sandbox):
    box, tmp_path = sandbox
    (tmp_path / "calc.py").write_text("a = 1\nb = 1\nc = 2\n")
    box.replace_in_file("calc.py", "c = 2", "c = 3")
    assert (tmp_path / "calc.py").read_text() == "a = 1\nb = 1\nc = 3\n"
    with pytest.raises(SandboxError, match="2 times"):
        box.replace_in_file("calc.py", "= 1", "= 5")
    with pytest.raises(SandboxError, match="0 times"):
        box.replace_in_file("calc.py", "zzz", "y")
    with pytest.raises(SandboxError):
        box.replace_in_file("../outside.py", "a", "b")


def test_ranged_reads_and_the_full_listing(sandbox):
    box, tmp_path = sandbox
    (tmp_path / "long.py").write_text("".join(f"line {n}\n" for n in range(1, 3001)))
    assert box.read_file("long.py", 10, 12) == "[lines 10-12 of 3000]\nline 10\nline 11\nline 12\n"
    assert len(box.read_text("long.py")) > 8000  # the worker's own reads aren't truncated
    (tmp_path / "pkg").mkdir()
    for n in range(600):  # far more than list_files' 8000 characters
        (tmp_path / "pkg" / f"module_with_a_long_name_{n}.py").write_text("")
    files = box.source_files()
    assert "long.py" in files and "pkg/module_with_a_long_name_599.py" in files
    assert len(files) == 602
