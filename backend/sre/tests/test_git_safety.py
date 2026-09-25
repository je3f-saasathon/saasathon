"""The sandbox can write anything in the work tree. None of it may make the worker
run code when it later runs git there."""

import subprocess
from types import SimpleNamespace

from sre.services.github import GitHubRepo, exclude_install_dirs


def _repo():
    project = SimpleNamespace(github_repo_owner="acme", github_repo_name="shop",
                              github_default_branch="main")
    return GitHubRepo(project)


def test_planted_git_hooks_and_config_do_not_run(tmp_path):
    git_dir, work_tree = tmp_path / "git", tmp_path / "tree"
    work_tree.mkdir()
    subprocess.run(["git", "init", "-q", f"--separate-git-dir={git_dir}", str(work_tree)], check=True)
    (work_tree / ".git").unlink()  # what GitHubRepo.clone does
    marker = tmp_path / "pwned"

    # A hook in the real git dir (the sandbox can't reach it, but belt and braces).
    hook = git_dir / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)

    # What a malicious agent could write: a fake .git with hooks and an fsmonitor command.
    fake = work_tree / ".git"
    (fake / "hooks").mkdir(parents=True)
    (fake / "hooks" / "pre-commit").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (fake / "hooks" / "pre-commit").chmod(0o755)
    (fake / "config").write_text(f"[core]\n\tfsmonitor = touch {marker}\n")
    (work_tree / "fix.py").write_text("x = 1\n")

    repo = _repo()
    assert repo.has_changes(git_dir, work_tree)
    repo._git(git_dir, work_tree, "add", "-A")
    repo._git(git_dir, work_tree, "-c", "user.name=t", "-c", "user.email=t@t",
              "commit", "-q", "-m", "fix")

    assert not marker.exists()
    committed = subprocess.run(["git", f"--git-dir={git_dir}", "show", "--name-only", "--format="],
                               capture_output=True, text=True, check=True).stdout
    assert "fix.py" in committed
    assert ".git/" not in committed


def test_installed_environments_are_never_committed(tmp_path):
    git_dir, work_tree = tmp_path / "git", tmp_path / "tree"
    work_tree.mkdir()
    subprocess.run(["git", "init", "-q", f"--separate-git-dir={git_dir}", str(work_tree)], check=True)
    (work_tree / ".git").unlink()
    exclude_install_dirs(git_dir)  # what GitHubRepo.clone does
    for path in (".venv/lib/site.py", "node_modules/pkg/index.js", "app/__pycache__/x.pyc"):
        (work_tree / path).parent.mkdir(parents=True, exist_ok=True)
        (work_tree / path).write_text("installed")
    (work_tree / "fix.py").write_text("x = 1\n")

    repo = _repo()
    repo._git(git_dir, work_tree, "add", "-A")
    repo._git(git_dir, work_tree, "-c", "user.name=t", "-c", "user.email=t@t",
              "commit", "-q", "-m", "fix")
    committed = subprocess.run(["git", f"--git-dir={git_dir}", "show", "--name-only", "--format="],
                               capture_output=True, text=True, check=True).stdout.split()
    assert committed == ["fix.py"]
