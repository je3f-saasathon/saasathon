"""buggly: run your app with its errors sent to buggly.

    buggly login                 sign in (opens the browser)
    buggly run python app.py     run a command; its crashes open incidents
    buggly status                who you are and which project this repo uses
    buggly logout
"""

import argparse
import os
import shutil
import sys
import time
import webbrowser

from . import config, gitinfo
from .api import ApiError, call
from .run import telemetry_env


def _fail(message: str, code: int = 1) -> int:
    print(f"buggly: {message}", file=sys.stderr)
    return code


def cmd_login(args) -> int:
    base = config.api_url(args.api_url)
    try:
        _, start = call(base, "POST", "/auth/cli/start")
    except ApiError as exc:
        return _fail(exc.detail)
    print(f"Your code: {start['user_code']}")
    print(f"Approve it at {start['verification_url']}")
    if not args.no_browser:
        webbrowser.open(start["verification_url"])
    deadline = time.monotonic() + start["expires_in"]
    while time.monotonic() < deadline:
        time.sleep(start["interval"])
        try:
            status, body = call(base, "POST", "/auth/cli/poll", {"device_code": start["device_code"]})
        except ApiError as exc:
            return _fail(exc.detail)
        if status == 200:
            config.save({**config.load(), "api_url": base, "token": body["token"]})
            print(f"Signed in as {body['user']['email']}.")
            return 0
    return _fail("the code expired: run `buggly login` again")


def cmd_logout(args) -> int:
    data = config.load()
    token = data.pop("token", "")
    if token:
        try:
            call(config.api_url(), "POST", "/auth/logout", token=token)
        except ApiError:
            pass  # forgetting it locally is what matters
        config.save(data)
    print("Signed out.")
    return 0


def _project(args) -> tuple[dict | None, str, str | None]:
    """(project, repo, error)."""
    token = config.token()
    if not token:
        return None, "", "not signed in: run `buggly login` first"
    repo = args.repo or gitinfo.github_repo()
    if not repo:
        return None, "", ("no GitHub remote here: run inside your repo's checkout, "
                          "or pass --repo owner/name")
    try:
        _, project = call(config.api_url(), "GET", "/sre/cli/project", token=token,
                          params={"repo": repo, "project_id": args.project})
    except ApiError as exc:
        if exc.status == 401:
            return None, repo, "your sign-in has expired: run `buggly login` again"
        if exc.status == 409:
            names = ", ".join(f"{p['id']} ({p['name']})" for p in exc.body.get("projects", []))
            return None, repo, f"{exc.detail}: {names}"
        return None, repo, exc.detail
    return project, repo, None


def cmd_status(args) -> int:
    token = config.token()
    if not token:
        return _fail("not signed in: run `buggly login`")
    try:
        _, me = call(config.api_url(), "GET", "/auth/me", token=token)
    except ApiError as exc:
        return _fail(exc.detail)
    print(f"Signed in to {config.api_url()} as {me['user']['email']}.")
    project, repo, error = _project(args)
    if error:
        return _fail(error)
    telemetry = "ready" if project["dsn"] else f"not ready ({project['uptrace_status'] or 'not managed'})"
    print(f"{repo} → project {project['project_id']} ({project['name']}), "
          f"service {project['service_name']}, telemetry {telemetry}.")
    return 0


def cmd_run(args) -> int:
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        return _fail("nothing to run: buggly run python app.py")
    if shutil.which(command[0]) is None:
        return _fail(f"command not found: {command[0]}", 127)
    project, repo, error = _project(args)
    if error:
        return _fail(error)
    if not project["dsn"]:
        _fail(f"{project['name']}'s telemetry isn't ready yet ({project['uptrace_status'] or 'not managed'}), "
              "so this run sends nothing. Try again in a minute.")
        env = dict(os.environ)
    else:
        env = telemetry_env(project, repo, gitinfo.commit(), dict(os.environ))
        print(f"buggly: sending {repo}'s errors to {project['name']}.", file=sys.stderr)
    # exec: the command gets our pid, its signals and its exit code as if run directly.
    os.execvpe(command[0], command, env)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="buggly", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    login = sub.add_parser("login", help="sign in through the browser")
    login.add_argument("--api-url", help=f"buggly API (default {config.DEFAULT_API_URL}, or BUGGLY_API_URL)")
    login.add_argument("--no-browser", action="store_true", help="just print the URL")
    login.set_defaults(func=cmd_login)

    sub.add_parser("logout", help="forget the saved token").set_defaults(func=cmd_logout)

    for name, func, text in (("status", cmd_status, "show the account and this repo's project"),
                             ("run", cmd_run, "run a command with its errors sent to buggly")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--repo", help="owner/name (default: from git remote)")
        p.add_argument("--project", type=int, help="project id, when several use the repo")
        if name == "run":
            p.add_argument("command", nargs=argparse.REMAINDER, help="the command, e.g. python app.py")
        p.set_defaults(func=func)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
