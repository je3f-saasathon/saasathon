#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: scripts/test-scripts.sh

Smoke test for the port/process contingency logic in lib/common.sh:
  - an unrelated process holding a port is reported, never killed without --force
  - our own tracked processes start/stop/restart cleanly
  - a stale pid file (dead/reused pid) is cleaned up silently
Exits non-zero on any failure.
EOF
}
[ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] && { usage; exit 0; }

FAIL=0
pass() { log_ok "$1"; }
fail() { log_err "$1"; FAIL=1; }

TEST_PORT=18173

cleanup_dummy() {
  if [ -n "${DUMMY_PID:-}" ] && kill -0 "$DUMMY_PID" 2>/dev/null; then
    kill -TERM "$DUMMY_PID" 2>/dev/null || true
    wait "$DUMMY_PID" 2>/dev/null || true
  fi
}
trap cleanup_dummy EXIT

# ---- 1: unrelated process holds our port, must not be killed without --force
log_info "test 1: unrelated process on port $TEST_PORT is reported, not killed"
python3 -m http.server "$TEST_PORT" >/dev/null 2>&1 &
DUMMY_PID=$!
sleep 1

if ensure_port_free "$TEST_PORT" 0; then
  fail "ensure_port_free (no --force) unexpectedly reported port free"
else
  if kill -0 "$DUMMY_PID" 2>/dev/null; then
    pass "unrelated process left alone without --force"
  else
    fail "unrelated process was killed without --force"
  fi
fi

if ensure_port_free "$TEST_PORT" 1; then
  if ! kill -0 "$DUMMY_PID" 2>/dev/null; then
    pass "--force freed the port by stopping the unrelated process"
  else
    fail "--force did not stop the unrelated process"
  fi
else
  fail "ensure_port_free --force did not report success"
fi
DUMMY_PID=""

# ---- 2: our own tracked process starts/stops/restarts cleanly
log_info "test 2: tracked process start/stop/restart"
start_bg testsvc "$LOG_DIR/testsvc.log" python3 -m http.server "$TEST_PORT"
sleep 1
pid=$(cat "$RUN_DIR/testsvc.pid" 2>/dev/null || echo "")
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
  pass "tracked process started (pid $pid)"
else
  fail "tracked process failed to start"
fi

stop_tracked testsvc
if [ -f "$RUN_DIR/testsvc.pid" ]; then
  fail "pid file not removed after stop_tracked"
else
  pass "pid file removed after stop_tracked"
fi
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
  fail "tracked process still alive after stop_tracked"
else
  pass "tracked process cleanly stopped"
fi

# restart
start_bg testsvc "$LOG_DIR/testsvc.log" python3 -m http.server "$TEST_PORT"
sleep 1
pid2=$(cat "$RUN_DIR/testsvc.pid" 2>/dev/null || echo "")
stop_tracked testsvc
if [ -n "$pid2" ] && ! kill -0 "$pid2" 2>/dev/null; then
  pass "restart cycle stopped cleanly"
else
  fail "restart cycle did not stop cleanly"
fi

# ---- 3: stale pid file cleaned up silently
log_info "test 3: stale pid file is cleaned up"
# pick a pid that's very unlikely to be alive/reused as ours
dead_pid=99999
while kill -0 "$dead_pid" 2>/dev/null; do dead_pid=$((dead_pid+1)); done
echo "$dead_pid" > "$RUN_DIR/staletest.pid"
stop_tracked staletest
if [ -f "$RUN_DIR/staletest.pid" ]; then
  fail "stale pid file was not cleaned up"
else
  pass "stale pid file cleaned up silently"
fi

rm -f "$LOG_DIR/testsvc.log"

echo
if [ "$FAIL" -eq 0 ]; then
  log_ok "all script contingency tests passed"
else
  log_err "one or more script contingency tests failed"
fi
exit $FAIL
