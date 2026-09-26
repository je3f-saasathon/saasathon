import gzip
import http.server
import shutil
import subprocess
import sys
import textwrap
import threading
from urllib.parse import unquote

import pytest

from buggly import cli, config
from buggly.gitinfo import parse_remote
from buggly.run import BOOTSTRAP_DIR, telemetry_env

DSN = "https://secret-token@uptrace.example.test?grpc=14317"


@pytest.mark.parametrize("url", [
    "https://github.com/acme/shop.git", "https://github.com/acme/shop", "git@github.com:acme/shop.git",
    "ssh://git@github.com/acme/shop", "https://user:tok@github.com/acme/shop.git/",
])
def test_parse_remote(url):
    assert parse_remote(url) == "acme/shop"


def test_parse_remote_rejects_other_hosts():
    assert parse_remote("https://gitlab.com/acme/shop.git") is None


def project(endpoint="https://uptrace.example.test", dsn=DSN):
    return {"project_id": 7, "name": "shop", "repo": "acme/shop", "service_name": "shop-api",
            "uptrace_status": "ready", "dsn": dsn, "otlp_endpoint": endpoint}


def test_telemetry_env():
    env = telemetry_env(project(), "acme/shop", "abc123",
                        {"PYTHONPATH": "/app", "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment=dev"})
    assert env["PYTHONPATH"].split(":") == [BOOTSTRAP_DIR, "/app"]
    assert env["OTEL_SERVICE_NAME"] == "shop-api"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
    name, _, value = env["OTEL_EXPORTER_OTLP_HEADERS"].partition("=")
    assert (name, unquote(value)) == ("uptrace-dsn", DSN)
    assert env["OTEL_RESOURCE_ATTRIBUTES"] == (
        "vcs.repository.url.full=https://github.com/acme/shop,service.version=abc123,"
        "deployment.environment=dev")


class Collector(http.server.BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        self.received.append((self.path, dict(self.headers), body))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def collector():
    Collector.received = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", Collector.received
    server.shutdown()


CRASH = textwrap.dedent("""
    def total(prices):
        return sum(prices) / len(prices)
    total([])
""")


def bare_python(tmp_path):
    """A venv with no OpenTelemetry, from another Python if there is one."""
    base = shutil.which("python3") or sys.executable
    subprocess.run([base, "-m", "venv", "--without-pip", str(tmp_path / "venv")], check=True)
    return str(tmp_path / "venv" / "bin" / "python")


@pytest.mark.parametrize("interpreter", ["cli", "bare"])
def test_an_uncaught_exception_reaches_uptrace(collector, tmp_path, interpreter):
    endpoint, received = collector
    python = sys.executable if interpreter == "cli" else bare_python(tmp_path)
    script = tmp_path / "app.py"
    script.write_text(CRASH)
    env = telemetry_env(project(endpoint=endpoint), "acme/shop", "abc123", {"PATH": "/usr/bin:/bin"})

    out = subprocess.run([python, str(script)], env=env, capture_output=True, text=True, timeout=60)

    # The app behaves as without buggly: same traceback, same exit code.
    assert out.returncode == 1
    assert "ZeroDivisionError: division by zero" in out.stderr
    assert "buggly: sent ZeroDivisionError to shop" in out.stderr
    traces = [(h, b) for path, h, b in received if path == "/v1/traces"]
    assert traces, out.stderr
    headers, body = traces[0]
    assert headers["uptrace-dsn"] == DSN
    for expected in (b"ZeroDivisionError", b"division by zero", b"shop-api", b"exception",
                     b"https://github.com/acme/shop", b"line 3, in total"):
        assert expected in body


def test_a_clean_exit_sends_nothing_extra(collector, tmp_path):
    endpoint, received = collector
    script = tmp_path / "ok.py"
    script.write_text("print('fine')")
    env = telemetry_env(project(endpoint=endpoint), "acme/shop", "", {"PATH": "/usr/bin:/bin"})
    out = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=60)
    assert (out.returncode, out.stdout, out.stderr) == (0, "fine\n", "")
    assert not any(b"exception" in body for _, _, body in received)


def test_the_apps_own_sitecustomize_still_runs(collector, tmp_path):
    endpoint, _ = collector
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    (theirs / "sitecustomize.py").write_text("import builtins; builtins.THEIRS = True")
    env = telemetry_env(project(endpoint=endpoint), "acme/shop", "",
                        {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(theirs)})
    out = subprocess.run([sys.executable, "-c", "print(THEIRS)"], env=env, capture_output=True,
                         text=True, timeout=60)
    assert out.stdout == "True\n", out.stderr


# ---- the command ------------------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("BUGGLY_TOKEN", raising=False)
    monkeypatch.delenv("BUGGLY_API_URL", raising=False)
    return tmp_path


def test_run_needs_a_login(home, capsys):
    assert cli.main(["run", "--repo", "acme/shop", "python", "-V"]) == 1
    assert "buggly login" in capsys.readouterr().err


def test_run_execs_the_command_with_the_projects_telemetry(home, monkeypatch):
    config.save({"api_url": "https://api.example.test", "token": "tok"})
    calls = []

    def fake_call(base, method, path, body=None, token="", params=None):
        calls.append((base, path, token, params))
        return 200, project()

    execs = []
    monkeypatch.setattr(cli, "call", fake_call)
    monkeypatch.setattr(cli.os, "execvpe", lambda f, argv, env: execs.append((argv, env)))
    cli.main(["run", "--repo", "acme/shop", "--", "python", "app.py"])

    assert calls == [("https://api.example.test", "/sre/cli/project", "tok",
                      {"repo": "acme/shop", "project_id": None})]
    argv, env = execs[0]
    assert argv == ["python", "app.py"]
    assert env["UPTRACE_DSN"] == DSN and env["BUGGLY_ACTIVE"] == "1"


def test_run_without_telemetry_ready_still_runs_the_app(home, monkeypatch, capsys):
    config.save({"token": "tok"})
    monkeypatch.setattr(cli, "call", lambda *a, **k: (200, project(dsn="", endpoint="") | {"uptrace_status": "provisioning"}))
    execs = []
    monkeypatch.setattr(cli.os, "execvpe", lambda f, argv, env: execs.append(env))
    cli.main(["run", "--repo", "acme/shop", "python", "app.py"])
    assert "BUGGLY_ACTIVE" not in execs[0]
    assert "isn't ready yet (provisioning)" in capsys.readouterr().err


def test_config_file_is_private(home):
    config.save({"token": "tok"})
    assert oct(config.config_path().stat().st_mode & 0o777) == "0o600"
    assert config.token() == "tok"
