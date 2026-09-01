#!/usr/bin/env sh
# First-time installer for yada on Linux.
#
# Sets up the versioned layout the updater expects, so that every subsequent release
# installs itself in the background with no further involvement:
#
#   ~/.local/share/yada/yada          the stable launcher (shortcuts point here)
#   ~/.local/share/yada/current       the active version
#   ~/.local/share/yada/versions/X/   this release
#
# Everything lives under the user's own profile: no sudo, and nothing outside $HOME.
set -eu

VERSION="${YADA_VERSION:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${YADA_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/yada}"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

if [ -z "$VERSION" ]; then
    # Fall back to asking the binary, so the installer works from a plain extracted archive.
    VERSION="$("$HERE/yada" --version 2>/dev/null | awk '{print $2}')" || VERSION=""
fi
if [ -z "$VERSION" ]; then
    echo "Could not determine the version. Set YADA_VERSION and retry." >&2
    exit 1
fi

echo "Installing yada $VERSION into $ROOT"
mkdir -p "$ROOT/versions/$VERSION" "$BIN_DIR" "$DESKTOP_DIR"

# The app itself.
cp -R "$HERE/_internal" "$ROOT/versions/$VERSION/"
cp "$HERE/yada" "$ROOT/versions/$VERSION/yada"
chmod +x "$ROOT/versions/$VERSION/yada"
# Written last: the launcher treats this marker as the only proof a version is usable.
printf '%s\n' "$VERSION" > "$ROOT/versions/$VERSION/.complete"

# The stable launcher, and the pointer at the active version.
cp "$HERE/yada-launcher" "$ROOT/yada"
chmod +x "$ROOT/yada"
printf '%s\n' "$VERSION" > "$ROOT/current"

# A symlink on PATH, which is also what `yada toggle` resolves to when you bind a shortcut.
ln -sf "$ROOT/yada" "$BIN_DIR/yada"

cat > "$DESKTOP_DIR/yada.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=yada
GenericName=Dictation
Comment=Press a shortcut, speak, get text
Exec=$ROOT/yada
Terminal=false
Categories=Utility;AudioVideo;
StartupNotify=false
DESKTOP

echo
echo "Installed. Start yada from your application menu, or run: $BIN_DIR/yada"
echo
echo "Next steps:"
echo "  1. Open Settings from the tray icon and paste an API key."
echo "  2. On Wayland, bind your shortcut in System Settings -> Shortcuts to:"
echo "       $ROOT/yada toggle"
echo "     (yada will try to register it via the desktop portal first, so you may not"
echo "      need to do this at all.)"
if ! printf '%s' "$PATH" | grep -q "$BIN_DIR"; then
    echo
    echo "Note: $BIN_DIR is not on your PATH. Add it if you want to run 'yada' directly."
fi
