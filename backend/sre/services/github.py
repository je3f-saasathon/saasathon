import base64
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from ..models import Project

# Never let repo-controlled config run code in the worker (which holds every secret).
SAFE_GIT_CONFIG = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "protocol.file.allow=never",
    "-c", "submodule.recurse=false",
]


class GitError(Exception):
    pass


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
            from github import Auth, GithubIntegration

            if not (settings.GITHUB_APP_ID and settings.GITHUB_APP_PRIVATE_KEY_PATH):
                raise ImproperlyConfigured("GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY_PATH not set")
            private_key = Path(settings.GITHUB_APP_PRIVATE_KEY_PATH).read_text()
            integration = GithubIntegration(auth=Auth.AppAuth(settings.GITHUB_APP_ID, private_key))
            self._token = integration.get_access_token(int(self.project.github_installation_id)).token
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
        self._git(git_dir, work_tree, "checkout", "-b", branch)

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

    def open_pull_request(self, branch: str, title: str, body: str) -> str:
        """Idempotent: returns the existing open PR for the branch if there is one."""
        from github import Auth, Github

        repo = Github(auth=Auth.Token(self.token())).get_repo(self.full_name)
        existing = repo.get_pulls(state="open", head=f"{self.project.github_repo_owner}:{branch}")
        for pr in existing:
            return pr.html_url
        pr = repo.create_pull(
            base=self.project.github_default_branch, head=branch, title=title, body=body
        )
        return pr.html_url
