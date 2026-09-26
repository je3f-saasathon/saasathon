"""`buggly run`: the environment that sends a command's telemetry to its project."""

import os
import sysconfig
from pathlib import Path
from urllib.parse import quote

BOOTSTRAP_DIR = str(Path(__file__).parent / "bootstrap")


def site_packages() -> list[str]:
    """Where this CLI's OpenTelemetry packages live, for the app's interpreter to fall back on."""
    paths = sysconfig.get_paths()
    return list(dict.fromkeys(p for p in (paths.get("purelib"), paths.get("platlib")) if p))


def _prepend(path: str, existing: str | None) -> str:
    return path + (os.pathsep + existing if existing else "")


def telemetry_env(project: dict, repo: str, commit: str, base: dict[str, str]) -> dict[str, str]:
    """base plus what makes every Python process the command starts send its traces, logs
    and uncaught exceptions to the project's Uptrace over OTLP/HTTP."""
    env = dict(base)
    dsn = project["dsn"]
    resource = [f"vcs.repository.url.full=https://github.com/{repo}"]
    if commit:
        resource.append(f"service.version={commit}")
    if base.get("OTEL_RESOURCE_ATTRIBUTES"):
        resource.append(base["OTEL_RESOURCE_ATTRIBUTES"])  # the app's own win
    env.update({
        "BUGGLY_ACTIVE": "1",
        "BUGGLY_PROJECT": project["name"],
        "BUGGLY_SITE_PACKAGES": os.pathsep.join(site_packages()),
        "PYTHONPATH": _prepend(BOOTSTRAP_DIR, base.get("PYTHONPATH")),
        "UPTRACE_DSN": dsn,
        "OTEL_SERVICE_NAME": project["service_name"],
        "OTEL_RESOURCE_ATTRIBUTES": ",".join(resource),
        # Uptrace's own SDK only speaks gRPC, which the platform's Uptrace doesn't accept.
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_ENDPOINT": project["otlp_endpoint"],
        "OTEL_EXPORTER_OTLP_HEADERS": "uptrace-dsn=" + quote(dsn, safe=""),
        "OTEL_EXPORTER_OTLP_COMPRESSION": "gzip",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        # Uptrace wants delta temporality; incidents only need traces.
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED": "true",
    })
    return env
