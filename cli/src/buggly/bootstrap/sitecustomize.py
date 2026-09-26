"""Loaded at startup by every Python process `buggly run` starts (it's first on PYTHONPATH).

It turns on OpenTelemetry auto-instrumentation with the CLI's own copy of the packages (added
to the *end* of sys.path, so an app that ships its own OpenTelemetry keeps it) and records an
uncaught exception on a span, sending it before the process exits: that exception event is
what opens an incident. It must never break the app, so every failure is a one-line warning.
"""

import os
import sys


def _warn(message: str) -> None:
    print(f"buggly: {message}", file=sys.stderr)


def _flush() -> None:
    from opentelemetry import _logs, trace

    for provider in (trace.get_tracer_provider(), _logs.get_logger_provider()):
        flush = getattr(provider, "force_flush", None)
        if flush is not None:
            flush(5000)


def _install_crash_hooks() -> None:
    import threading

    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode

    tracer = trace.get_tracer("buggly")
    project = os.environ.get("BUGGLY_PROJECT", "buggly")

    def record(exc: BaseException) -> None:
        try:
            with tracer.start_as_current_span(f"uncaught {type(exc).__name__}",
                                              record_exception=False) as span:
                span.record_exception(exc, escaped=True)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
            _flush()
            _warn(f"sent {type(exc).__name__} to {project}; an incident opens in about a minute.")
        except Exception as err:  # never hide the app's own traceback
            _warn(f"could not send the crash: {err}")

    previous = sys.excepthook

    def excepthook(exc_type, exc, tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            record(exc)
        previous(exc_type, exc, tb)

    sys.excepthook = excepthook

    previous_thread = threading.excepthook

    def thread_excepthook(args):
        if args.exc_value is not None and not issubclass(args.exc_type, SystemExit):
            record(args.exc_value)
        previous_thread(args)

    threading.excepthook = thread_excepthook


def _setup() -> None:
    for path in os.environ.get("BUGGLY_SITE_PACKAGES", "").split(os.pathsep):
        if path and path not in sys.path:
            sys.path.append(path)
    from opentelemetry.instrumentation.auto_instrumentation import initialize

    initialize()
    _install_crash_hooks()


def _chain() -> None:
    """Runs the sitecustomize this one shadows (Debian/Ubuntu Pythons ship one)."""
    here = os.path.dirname(os.path.abspath(__file__))
    saved = sys.path[:]
    ours = sys.modules.pop("sitecustomize", None)
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != here]
    try:
        import sitecustomize  # noqa: F401
    except ImportError:
        if ours is not None:
            sys.modules["sitecustomize"] = ours
    except Exception as exc:
        _warn(f"the app's own sitecustomize failed: {exc}")
    finally:
        sys.path[:] = saved


_chain()
if os.environ.get("BUGGLY_ACTIVE") == "1":
    try:
        _setup()
    except Exception as exc:
        _warn(f"telemetry is off in this process: {exc}")
