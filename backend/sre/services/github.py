import base64
import shutil
import subprocess
from pathlib import Path

from ..models import Project
from .github_connect import installation_token

# Never let repo-controlled config run code in the worker (which holds every secret).
SAFE_GIT_CONFIG = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "protocol.file.allow=never",
    "-c", "submodule.recurse=false",
]


AWAITING_APPROVAL_PREFIX = "[Awaiting approval] "
AWAITING_APPROVAL_NOTE = (
    "> **Fix proposed by the SRE agent, awaiting review.** This repo's plan doesn't allow "
    "draft PRs, so this PR is marked by its title instead. Merging it accepts the fix; "
    "closing it without merging rejects it.\n\n"
)


class GitError(Exception):
    pass


INSTALL_DIRS = [".venv/", "node_modules/", "__pycache__/", ".pytest_cache/"]


def exclude_install_dirs(git_dir: Path) -> None:
    """Environments the dependency install creates must never end up in the PR, whatever
    the repo's own .gitignore says. info/exclude lives in the git dir, out of the sandbox's
    reach, and only affects untracked files."""
    (git_dir / "info").mkdir(parents=True, exist_ok=True)
    with open(git_dir / "info" / "exclude", "a") as exclude:
        exclude.write("\n" + "\n".join(INSTALL_DIRS) + "\n")


class GitHubRepo:
    """Worker-side git/GitHub operations. The installation token never touches disk
    and never enters the sandbox: it's passed as a one-off http header."""

    def __init__(self, project: Project):
        self.project = project
        self.full_name = f"{project.github_repo_owner}/{project.github_repo_name}"
        self.url = f"https://github.com/{self.full_name}.git"
        self._token = None

    def token(self) -> str:
        if self._token is None:
            self._token = installation_token(self.project.github_installation_id)
        return self._token

    def _auth_config(self) -> list[str]:
        basic = base64.b64encode(f"x-access-token:{self.token()}".encode()).decode()
        return ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]

    def _git(self, git_dir: Path, work_tree: Path, *args: str, auth: bool = False) -> str:
        cmd = ["git", *SAFE_GIT_CONFIG, *(self._auth_config() if auth else []),
               f"--git-dir={git_dir}", f"--work-tree={work_tree}", *args]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            # Don't echo the command: it contains the auth header.
            raise GitError(f"git {args[0]} failed: {result.stderr.strip()[-2000:]}")
        return result.stdout

    def clone(self, git_dir: Path, work_tree: Path, branch: str) -> None:
        """Git metadata lives outside the work tree so the sandbox (which only mounts the
        work tree) can't plant hooks or config that the worker would later execute."""
        cmd = ["git", *SAFE_GIT_CONFIG, *self._auth_config(), "clone", "--depth", "50",
               "--branch", self.project.github_default_branch,
               f"--separate-git-dir={git_dir}", self.url, str(work_tree)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise GitError(f"git clone failed: {result.stderr.strip()[-2000:]}")
        # The separate-git-dir pointer file is inside the sandbox's reach; drop it.
        # Every later git call passes --git-dir explicitly.
        (work_tree / ".git").unlink(missing_ok=True)
        exclude_install_dirs(git_dir)
        self._git(git_dir, work_tree, "checkout", "-b", branch)

    def clone_snapshot(self, dest: Path, branch: str = "") -> None:
        """A copy of a branch (default: the default branch) for an agent to read: files
        only, no git metadata, so nothing in it is ever run or pushed by the worker."""
        cmd = ["git", *SAFE_GIT_CONFIG, *self._auth_config(), "clone", "--depth", "1",
               "--single-branch", "--no-tags", "--branch", branch or self.project.github_default_branch,
               self.url, str(dest)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise GitError(f"git clone failed: {result.stderr.strip()[-2000:]}")
        shutil.rmtree(dest / ".git", ignore_errors=True)

    def compare(self, base: str, head: str, max_files: int = 100) -> list[dict]:
        """The files a push or merge changed, with their patches (GitHub's compare API)."""
        comparison = self._repo().compare(base, head)
        return [{"path": f.filename, "status": f.status, "patch": (f.patch or "")[:6000]}
                for f in list(comparison.files)[:max_files]]

    def has_changes(self, git_dir: Path, work_tree: Path) -> bool:
        return bool(self._git(git_dir, work_tree, "status", "--porcelain").strip())

    def diff(self, git_dir: Path, work_tree: Path) -> str:
        self._git(git_dir, work_tree, "add", "-A")
        return self._git(git_dir, work_tree, "diff", "--cached", "--stat")

    def commit_and_push(self, git_dir: Path, work_tree: Path, branch: str, message: str) -> None:
        self._git(git_dir, work_tree, "add", "-A")
        self._git(git_dir, work_tree, "-c", "user.name=SRE Agent",
                  "-c", "user.email=sre-agent@users.noreply.github.com",
                  "commit", "-m", message)
        self._git(git_dir, work_tree, "push", "--force", "origin", f"HEAD:refs/heads/{branch}",
                  auth=True)

    def _repo(self):
        from github import Auth, Github

        return Github(auth=Auth.Token(self.token())).get_repo(self.full_name)

    def _open_pr(self, repo, branch: str):
        for pr in repo.get_pulls(state="open", head=f"{self.project.github_repo_owner}:{branch}"):
            return pr
        return None

    def open_pull_request(self, branch: str, title: str, body: str, draft: bool = False) -> str:
        """Idempotent: returns the existing open PR for the branch if there is one.
        With draft=True it opens a draft PR, or, where the plan doesn't allow drafts
        (private repos on free plans), a normal PR marked as awaiting approval."""
        from github import GithubException

        repo = self._repo()
        existing = self._open_pr(repo, branch)
        if existing is not None:
            return existing.html_url
        base = self.project.github_default_branch
        if not draft:
            return repo.create_pull(base=base, head=branch, title=title, body=body).html_url
        try:
            return repo.create_pull(base=base, head=branch, title=title, body=body, draft=True).html_url
        except GithubException as exc:
            if exc.status != 422 or "draft" not in str(exc.data).lower():
                raise
        return repo.create_pull(
            base=base, head=branch, title=AWAITING_APPROVAL_PREFIX + title,
            body=AWAITING_APPROVAL_NOTE + body,
        ).html_url

    def mark_pull_request_approved(self, branch: str) -> str | None:
        """Draft → ready for review (or drop the awaiting-approval marker). None if no open PR."""
        repo = self._repo()
        pr = self._open_pr(repo, branch)
        if pr is None:
            return None
        if pr.draft:
            pr.mark_ready_for_review()
        if pr.title.startswith(AWAITING_APPROVAL_PREFIX):
            pr.edit(title=pr.title[len(AWAITING_APPROVAL_PREFIX):],
                    body=(pr.body or "").replace(AWAITING_APPROVAL_NOTE, ""))
        return pr.html_url

    def close_pull_request(self, branch: str, comment: str) -> None:
        repo = self._repo()
        pr = self._open_pr(repo, branch)
        if pr is not None:
            pr.create_issue_comment(comment)
            pr.edit(state="closed")
