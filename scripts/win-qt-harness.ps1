# Build a throwaway Windows Qt harness, for reproducing Windows-only UI bugs.
#
# Why this exists: yada's UI bugs have overwhelmingly been Windows-only -- clipped
# checkmarks, a spin box whose arrows kept the style's native metric, and a language
# dropdown that opened with a zero-height viewport. Every one of them was invisible on
# Linux, and the language popup was "fixed" twice from a Linux screenshot before anyone
# reproduced it. Rendering has to be checked on the platform that renders it.
#
# It is deliberately temporary and ~700 MB, so it is created on demand and deleted after.
#
#   powershell -ExecutionPolicy Bypass -File scripts/win-qt-harness.ps1 -Script probe.py
#
# `-Script` runs with yada's `src/` on sys.path, so it can import yada.ui directly:
#
#   from yada.ui.settings_window import SettingsWindow   # the real window
#   from yada.ui.theme import apply_theme                # the real stylesheet
#
# Measure geometry rather than eyeballing a screenshot -- `view.height()`,
# `view.viewport().height()`, `style.subControlRect(...)`. The language popup reported
# 38-pixel rows while its viewport was 0, which no screenshot would have explained.

param(
    [string]$Script = "",
    [string]$Root = "$env:TEMP\yada-qt",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

if ($Remove) {
    Remove-Item $Root -Recurse -Force -ErrorAction SilentlyContinue
    Write-Output "removed $Root"
    exit 0
}

$python = Join-Path $Root "Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Output "creating venv in $Root ..."
    py -3 -m venv $Root
    # PySide6 is the point; the rest are what yada's UI modules import on the way in.
    & $python -m pip install --quiet --disable-pip-version-check `
        PySide6 platformdirs numpy soxr httpx websockets keyring
}

# Copied rather than referenced: the repo usually lives in WSL, and Qt on Windows reading
# source over \\wsl$ is slow enough to change what you are measuring.
$src = Join-Path $Root "src"
$repo = Split-Path -Parent $PSScriptRoot
Remove-Item (Join-Path $src "yada") -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $src -Force | Out-Null
Copy-Item (Join-Path $repo "src\yada") (Join-Path $src "yada") -Recurse -Force

& $python -c "import PySide6; print('PySide6', PySide6.__version__, 'ready in', r'$Root')"
if ($Script) { & $python $Script }
