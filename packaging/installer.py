"""The double-click installer.

Built as a console binary named INSTALL.exe (or INSTALL on Linux) and shipped at the top
level of the release archive, so installing is: unzip, double-click, done. No PowerShell,
no execution policy, no command line.

It sets up the versioned layout the updater expects, entirely under the user's own profile:

    <root>/yada[.exe]        stable launcher — shortcuts point here, never changes
    <root>/current           the active version
    <root>/versions/<v>/     this release

Deliberately stdlib-only so it can be frozen into a tiny binary with no Qt, no numpy and
no network stack. It must work even when the app it is installing would not.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

APP = "yada"
IS_WINDOWS = sys.platform == "win32"
EXE = f"{APP}.exe" if IS_WINDOWS else APP
LAUNCHER_SRC = f"{APP}-launcher.exe" if IS_WINDOWS else f"{APP}-launcher"


def payload_dir() -> Path:
    """Where the files to install live: next to this executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def install_root() -> Path:
    if override := os.environ.get("YADA_INSTALL_ROOT"):
        return Path(override)
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP
    return Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / APP


def read_version(here: Path) -> str:
    """CI writes VERSION into every archive; it is the only dependable source.

    Asking the binary cannot work on Windows: yada.exe is built for the GUI subsystem and
    has no stdout, so `yada.exe --version` returns an empty string.
    """
    if env := os.environ.get("YADA_VERSION"):
        return env.strip()
    version_file = here / "VERSION"
    if version_file.exists():
        return version_file.read_text(encoding="utf-8").strip()
    raise SystemExit(
        "Could not determine the version: no VERSION file next to this installer.\n"
        "Re-download the release archive and try again."
    )


def install(here: Path, root: Path, version: str) -> None:
    target = root / "versions" / version
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    print(f"  copying application files to {target}")
    internal = here / "_internal"
    if internal.is_dir():
        shutil.copytree(internal, target / "_internal", dirs_exist_ok=True)
    shutil.copy2(here / EXE, target / EXE)
    if not IS_WINDOWS:
        (target / EXE).chmod(0o755)

    # A copy of the launcher lives beside the app in every version directory, so the app
    # can refresh the stable one at the install root when a release changes it. Updates
    # only ever write into versions/<v>/, so without this the root launcher installed
    # today would never be replaced.
    launcher_source = here / LAUNCHER_SRC
    if launcher_source.is_file():
        shutil.copy2(launcher_source, target / LAUNCHER_SRC)

    # Written last: the launcher treats this marker as the only proof a version is usable,
    # so an interrupted install is ignored rather than half-booted.
    (target / ".complete").write_text(version + "\n", encoding="utf-8")

    # No launcher binary is installed at the root any more. The one that used to live
    # there was a one-file PyInstaller executable, and Windows Defender classified it as
    # Trojan:Win32/Bearfoos.A!ml -- a machine-learning heuristic, not a signature -- then
    # removed it along with the Start Menu shortcut and the autostart key. The one-dir
    # application was never touched. Shortcuts now point straight at a version's own
    # executable, and the running version keeps them current.
    (root / "current").write_text(version + "\n", encoding="utf-8")


def add_windows_integration(root: Path, version: str) -> None:
    launcher = root / "versions" / version / EXE

    # Start Menu shortcut, pointing at this version's executable. The running app rewrites
    # it whenever a newer version takes over, which is what used to be the launcher's job.
    start_menu = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs"
    start_menu.mkdir(parents=True, exist_ok=True)
    lnk = start_menu / f"{APP}.lnk"
    script = (
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
        f"$s.TargetPath='{launcher}';$s.WorkingDirectory='{root}';"
        "$s.Description='Press a shortcut, speak, get text';$s.Save()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            check=True,
            capture_output=True,
            timeout=30,
        )
        print(f"  Start Menu shortcut: {lnk.name}")
    except (OSError, subprocess.SubprocessError) as exc:
        # A missing shortcut is cosmetic; the install is still usable.
        print(f"  note: could not create the Start Menu shortcut ({exc})")

    # Autostart is deliberately NOT registered here. Writing HKCU\...\Run from a binary
    # that was created seconds ago is a significant part of the behavioural profile that
    # got the old launcher quarantined -- Defender remediated the run key alongside the
    # file. yada offers "start when I log in" as a setting instead, applied by the running
    # application rather than by an installer at drop time.
    print("  autostart is available as a setting inside yada")


def add_linux_integration(root: Path, version: str) -> None:
    launcher = root / "versions" / version / EXE
    bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    link = bin_dir / APP
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(launcher)
        print(f"  linked {link}")
    except OSError as exc:
        print(f"  note: could not link into ~/.local/bin ({exc})")

    apps = Path(
        os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    ) / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / f"{APP}.desktop").write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP}\n"
        "GenericName=Dictation\n"
        "Comment=Press a shortcut, speak, get text\n"
        f"Exec={launcher}\n"
        "Terminal=false\n"
        "Categories=Utility;AudioVideo;\n"
        "StartupNotify=false\n",
        encoding="utf-8",
    )
    print(f"  desktop entry: {apps / (APP + '.desktop')}")


def launch(root: Path, version: str) -> bool:
    """Start yada, so installing a tray app does something visible.

    Without this the installer finishes, nothing appears, and the only clue is a Start Menu
    entry the user has no reason to look for. Detached on purpose: the installer must not
    wait for the app it just started.
    """
    launcher = root / "versions" / version / EXE
    try:
        if IS_WINDOWS:
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, so closing this console window
            # does not take the app with it.
            subprocess.Popen(
                [str(launcher)],
                close_fds=True,
                creationflags=0x00000008 | 0x00000200,
            )
        else:
            subprocess.Popen(
                [str(launcher)],
                close_fds=True,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  note: could not start yada automatically ({exc})")
        return False
    return True


def main() -> int:
    here = payload_dir()
    print("yada installer")
    print("=" * 52)
    try:
        version = read_version(here)
        root = install_root()
        print(f"\nInstalling yada {version}")
        print(f"into {root}\n")

        missing = [n for n in (EXE,) if not (here / n).exists()]
        if missing:
            raise SystemExit(
                "This installer is missing files it needs: "
                + ", ".join(missing)
                + "\nExtract the whole archive before running it, rather than copying "
                "the installer out on its own."
            )

        install(here, root, version)
        if IS_WINDOWS:
            add_windows_integration(root, version)
        else:
            add_linux_integration(root, version)

        print("\n  starting yada")
        started = launch(root, version)

        print("\n" + "=" * 52)
        print("Installed." if not started else "Installed and running.")
        if IS_WINDOWS:
            print()
            print("  LOOK FOR THE TRAY ICON BEHIND THE ^ ARROW on your taskbar.")
            print("  Windows 11 hides new tray icons there by default. To keep it visible,")
            print("  drag it out, or turn on Settings > Personalisation > Taskbar >")
            print("  'Other system tray icons'.")
            print()
            print("  yada is also in your Start Menu and starts with Windows from now on.")
            print("  Your shortcut is Ctrl+Shift+; and is registered automatically.")
        else:
            print(f"\n  Run it with: {Path.home() / '.local' / 'bin' / APP}")
            print("  On Wayland, bind Ctrl+Shift+; in System Settings to:")
            print(f"      {Path.home() / '.local' / 'bin' / APP} toggle")
        print("\n  Open Settings from the tray icon and paste an API key to begin.")
        return 0
    except SystemExit as exc:
        print(f"\nInstall failed: {exc}")
        return 1
    except Exception:  # noqa: BLE001 - the traceback is the useful output for a bug report
        print("\nInstall failed unexpectedly:\n")
        traceback.print_exc()
        return 1
    finally:
        # Double-clicked from Explorer the console closes instantly on exit, taking any
        # error message with it, so pause for a human. But never in automation: Git Bash on
        # a Windows CI runner reports stdin as a tty, and a blocking prompt there hangs the
        # job until it is killed hours later.
        automated = os.environ.get("CI") or os.environ.get("YADA_INSTALLER_NO_PAUSE")
        if IS_WINDOWS and not automated and sys.stdin is not None and sys.stdin.isatty():
            with contextlib.suppress(EOFError, KeyboardInterrupt):
                input("\nPress Enter to close…")


if __name__ == "__main__":
    raise SystemExit(main())
