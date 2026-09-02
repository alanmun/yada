# First-time installer for yada on Windows 11.
#
# Sets up the versioned layout the updater expects, so later releases install themselves
# silently in the background:
#
#   %LOCALAPPDATA%\yada\yada.exe          the stable launcher (shortcuts point here)
#   %LOCALAPPDATA%\yada\current           the active version
#   %LOCALAPPDATA%\yada\versions\X\       this release
#
# Per-user, so no administrator rights are needed and nothing touches Program Files.
$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = if ($env:YADA_INSTALL_ROOT) { $env:YADA_INSTALL_ROOT } else { Join-Path $env:LOCALAPPDATA 'yada' }

# Version, in order of reliability. The VERSION file is written by CI into every archive
# and is the dependable source. Asking the binary cannot work here: yada.exe is built for
# the Windows GUI subsystem so it has no stdout, and `& yada.exe --version` returns nothing.
$version = $env:YADA_VERSION
if (-not $version) {
    $versionFile = Join-Path $here 'VERSION'
    if (Test-Path $versionFile) { $version = (Get-Content $versionFile -Raw).Trim() }
}
if (-not $version) {
    throw "Could not determine the version: no VERSION file next to this script. Re-download the release archive, or set `$env:YADA_VERSION and retry."
}

Write-Host "Installing yada $version into $root"
$versionDir = Join-Path $root "versions\$version"
New-Item -ItemType Directory -Force -Path $versionDir | Out-Null

Copy-Item (Join-Path $here '_internal') -Destination $versionDir -Recurse -Force
Copy-Item (Join-Path $here 'yada.exe') -Destination (Join-Path $versionDir 'yada.exe') -Force
# Written last: the launcher treats this marker as the only proof a version is usable.
Set-Content -Path (Join-Path $versionDir '.complete') -Value $version -NoNewline

Copy-Item (Join-Path $here 'yada-launcher.exe') -Destination (Join-Path $root 'yada.exe') -Force
Set-Content -Path (Join-Path $root 'current') -Value $version -NoNewline

# Start Menu shortcut, pointing at the launcher so it survives every future update.
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$shortcut = Join-Path $startMenu 'yada.lnk'
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($shortcut)
$link.TargetPath = Join-Path $root 'yada.exe'
$link.WorkingDirectory = $root
$link.Description = 'Press a shortcut, speak, get text'
$link.Save()

# Start with Windows, since a tray dictation tool is only useful if it is already running.
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
Set-ItemProperty -Path $runKey -Name 'yada' -Value ('"' + (Join-Path $root 'yada.exe') + '"')

Write-Host ''
Write-Host 'Installed. yada is in your Start Menu and will start with Windows.'
Write-Host 'Open Settings from the tray icon and paste an API key to begin.'
Write-Host 'Your shortcut (default Ctrl+Shift+;) is registered automatically on Windows.'
