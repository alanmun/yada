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

echo "=== 1. the application binary starts and can print ==="
# Also proves the Windows console-attach path: yada.exe is a GUI-subsystem binary and
# would otherwise emit nothing at all here.
out="$("$PAYLOAD/yada$EXE" --version 2>&1 || true)"
echo "     --version -> ${out:-<nothing>}"
[ -n "$out" ] || fail "--version printed nothing (console attach or startup is broken)"
case "$out" in *"$VERSION"*) pass "reports version $VERSION" ;; *) fail "expected $VERSION" ;; esac

echo "=== 2. the full import graph loads ==="
# doctor touches Qt, numpy, soxr, sounddevice, httpx and keyring. Exit code 1 is expected
# and fine: a CI runner has no microphone. Silence is not.
set +e
doctor="$("$PAYLOAD/yada$EXE" doctor 2>&1)"
doctor_rc=$?
set -e
echo "$doctor" | sed 's/^/     /'
[ -n "$doctor" ] || fail "doctor printed nothing"
case "$doctor" in *"yada doctor"*) pass "doctor ran (exit $doctor_rc)" ;; *) fail "unexpected doctor output" ;; esac

echo "=== 3. the double-click installer works ==="
( cd "$PAYLOAD" && ./INSTALL$EXE ) | sed 's/^/     /'
[ -f "$YADA_INSTALL_ROOT/current" ] || fail "no current pointer was written"
[ -f "$YADA_INSTALL_ROOT/versions/$VERSION/.complete" ] || fail "version was not marked complete"
[ -x "$YADA_INSTALL_ROOT/yada$EXE" ] || [ -f "$YADA_INSTALL_ROOT/yada$EXE" ] || fail "launcher not installed"
pass "installed $(cat "$YADA_INSTALL_ROOT/current")"

echo "=== 4. the installed app boots, answers IPC, and shuts down ==="
# Output goes to a file, not the step's pipe. A backgrounded GUI process that inherits the
# runner's stdout keeps the handle open, and the step then hangs long after the script is
# finished -- which is exactly how this cost a six-hour Windows job.
APP_LOG="$WORK/app.log"
"$YADA_INSTALL_ROOT/yada$EXE" > "$APP_LOG" 2>&1 &
APP_PID=$!
show_app_log() {
  echo "     --- app output ---"
  sed 's/^/     /' "$APP_LOG" 2>/dev/null || echo "     (no output captured)"
}
started=0
for _ in $(seq 1 60); do
  if "$YADA_INSTALL_ROOT/yada$EXE" status >/dev/null 2>&1; then started=1; break; fi
  sleep 0.5
done
if [ "$started" != "1" ]; then
  echo "     app never answered its socket after 30s"
  show_app_log
  kill "$APP_PID" 2>/dev/null || true
  fail "app did not start"
fi
pass "app started and answered status"

"$YADA_INSTALL_ROOT/yada$EXE" stop >/dev/null 2>&1 || fail "stop command failed"
stopped=0
for _ in $(seq 1 20); do
  if ! "$YADA_INSTALL_ROOT/yada$EXE" status >/dev/null 2>&1; then stopped=1; break; fi
  sleep 0.5
done
if [ "$stopped" != "1" ]; then
  show_app_log
  kill "$APP_PID" 2>/dev/null || true
  fail "app did not shut down"
fi
pass "app shut down cleanly"
# Belt and braces: never leave a process holding the runner's handles.
wait "$APP_PID" 2>/dev/null || true

echo
echo "SMOKE TEST PASSED — the built binaries run on $(uname -s)"
