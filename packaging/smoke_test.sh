#!/usr/bin/env bash
# Prove the freshly built binaries actually run, on the OS that built them.
#
# Runs in CI before anything is published. Catches the class of failure that unit tests
# cannot: a frozen binary whose import graph is incomplete, an installer that cannot find
# its payload, a GUI subsystem app that prints nothing, or an app that starts and then
# fails to answer its own IPC socket.
#
# Usage: bash packaging/smoke_test.sh <version>
set -euo pipefail

VERSION="${1:?usage: smoke_test.sh <version>}"

case "$(uname -s)" in
  MINGW* | MSYS* | CYGWIN*) EXE=".exe" ;;
  *)
    EXE=""
    # No display on a Linux runner. Windows always has a window station, so there the
    # real platform plugin is exercised rather than a stand-in.
    export QT_QPA_PLATFORM=offscreen
    ;;
esac

# Overridable so the script itself can be exercised outside CI.
PAYLOAD="${YADA_PAYLOAD:-dist/yada}"
WORK="${YADA_SMOKE_WORK:-$(pwd)/smoke}"
rm -rf "$WORK"
mkdir -p "$WORK"
export YADA_INSTALL_ROOT="$WORK/install"
export YADA_RUNTIME_DIR="$WORK/run"
# Fully isolated. The installer also writes a launcher symlink and a desktop entry into
# HOME, which must not leak onto a developer machine running this by hand.
export HOME="$WORK/home"
export XDG_DATA_HOME="$WORK/home/.local/share"
export XDG_CONFIG_HOME="$WORK/home/.config"
export XDG_CACHE_HOME="$WORK/home/.cache"
mkdir -p "$HOME"

export YADA_INSTALLER_NO_PAUSE=1  # the installer must never block on input here

pass() { echo "  ok   $1"; }
fail() { echo "  FAIL $1" >&2; exit 1; }

# Every invocation is bounded and streamed.
#
# Bounded, because a wedged binary should cost seconds and a clear message, not the whole
# step budget. Streamed via tee rather than captured into a variable, because command
# substitution shows nothing until the process exits -- a hang then produces a completely
# silent failure, which is how two Windows runs were burned learning nothing.
run_logged() {
  local secs="$1" log="$2"
  shift 2
  local rc=0
  if command -v timeout >/dev/null 2>&1; then
    timeout "$secs" "$@" 2>&1 | tee "$log" || rc=${PIPESTATUS[0]}
  else
    "$@" 2>&1 | tee "$log" || rc=${PIPESTATUS[0]}
  fi
  if [ "$rc" = "124" ]; then
    echo "     (timed out after ${secs}s)"
  fi
  return "$rc"
}

echo "=== 1. the application binary starts and can print ==="
# Also proves the Windows console-attach path: yada.exe is a GUI-subsystem binary and
# would otherwise emit nothing at all here.
run_logged 60 "$WORK/version.log" "$PAYLOAD/yada$EXE" --version || true
out="$(cat "$WORK/version.log" 2>/dev/null || true)"
[ -n "$out" ] || fail "--version printed nothing (console attach or startup is broken)"
case "$out" in *"$VERSION"*) pass "reports version $VERSION" ;; *) fail "expected $VERSION, got: $out" ;; esac

echo "=== 2. the full import graph loads ==="
# doctor touches Qt, numpy, soxr, sounddevice, httpx and keyring. Exit code 1 is expected
# and fine: a CI runner has no microphone. Silence is not.
set +e
run_logged 120 "$WORK/doctor.log" "$PAYLOAD/yada$EXE" doctor
doctor_rc=$?
set -e
doctor="$(cat "$WORK/doctor.log" 2>/dev/null || true)"
[ -n "$doctor" ] || fail "doctor printed nothing at all"
case "$doctor" in
  *"yada doctor"*) : ;;
  *) fail "doctor produced unexpected output" ;;
esac
# Exit 0 means everything is fine, 1 means it found problems -- both are correct outcomes
# on a machine with no microphone. Anything else means doctor itself broke, and an earlier
# version of this check accepted a timeout (124) as a pass, hiding a crash for two releases.
case "$doctor_rc" in
  0 | 1) : ;;
  124) fail "doctor timed out (exit 124) — it hung instead of reporting" ;;
  *) fail "doctor exited $doctor_rc, which means doctor itself failed" ;;
esac
case "$doctor" in
  *Traceback*) fail "doctor raised an exception (see the report above)" ;;
esac
pass "doctor ran cleanly (exit $doctor_rc)"

echo "=== 3. the double-click installer works ==="
( cd "$PAYLOAD" && run_logged 120 "$WORK/install.log" "./INSTALL$EXE" )
[ -f "$YADA_INSTALL_ROOT/current" ] || fail "no current pointer was written"
[ -f "$YADA_INSTALL_ROOT/versions/$VERSION/.complete" ] || fail "version was not marked complete"
VERSION_EXE="$YADA_INSTALL_ROOT/versions/$VERSION/yada$EXE"
[ -f "$VERSION_EXE" ] || fail "the installed version has no executable"
# There must be no launcher binary at the install root: the one-file build that used to
# live there was quarantined by Defender as Trojan:Win32/Bearfoos.A!ml.
[ ! -e "$YADA_INSTALL_ROOT/yada$EXE" ] || fail "a launcher binary was installed at the root"
pass "installed $(cat "$YADA_INSTALL_ROOT/current") with no root launcher"

echo "=== 4. the app the installer started answers IPC, and shuts down ==="
# The installer launches yada itself, so this checks that instance rather than starting a
# second one. An earlier version raced its own copy against the installer's: the second
# process could not bind the command socket, so stopping the first left the second alive
# holding the step open until the job timed out.
APP_LOG="$WORK/app.log"
show_app_log() {
  echo "     --- app output ---"
  sed 's/^/     /' "$APP_LOG" 2>/dev/null || echo "     (no output captured)"
}

started=0
for _ in $(seq 1 60); do
  if "$VERSION_EXE" status >/dev/null 2>&1; then started=1; break; fi
  sleep 0.5
done
if [ "$started" != "1" ]; then
  echo "     the installer said it started yada, but nothing answered in 30s"
  show_app_log
  fail "app did not start"
fi
pass "app started by the installer answers status"

"$VERSION_EXE" stop >/dev/null 2>&1 || fail "stop command failed"
stopped=0
for _ in $(seq 1 40); do
  if ! "$VERSION_EXE" status >/dev/null 2>&1; then stopped=1; break; fi
  sleep 0.5
done
if [ "$stopped" != "1" ]; then
  echo "     still answering status 20s after stop"
  show_app_log
  if command -v tasklist >/dev/null 2>&1; then
    tasklist //FI "IMAGENAME eq yada.exe" 2>/dev/null | sed 's/^/     /' || true
  else
    ps -ef 2>/dev/null | grep -i "[y]ada" | sed 's/^/     /' || true
  fi
  fail "app did not shut down"
fi
pass "app shut down cleanly"

echo
echo "SMOKE TEST PASSED — the built binaries run on $(uname -s)"
