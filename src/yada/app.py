"""Application wiring.

Threading model, which everything here depends on:

* **Qt main thread** owns every widget, the tray, and the settings window.
* **One asyncio loop on a worker thread** owns every network call -- realtime sockets, HTTP,
  update downloads. Nothing here blocks the UI, so a slow transform cannot freeze the tray.
* **PortAudio's callback thread** delivers audio and is never allowed to block.

Crossing between them is always a Qt signal (queued automatically for cross-thread
connections) or `run_coroutine_threadsafe`. No shared mutable state.

Two lines below are the direct fix for what prompted this project:
`setQuitOnLastWindowClosed(False)` plus the settings window's `closeEvent` override, which
together mean closing a window hides it and only the tray's Quit exits.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication

from . import config, ipc, secrets
from .audio import AudioCapture, AudioDeviceError, peak_level
from .audio import warm_up as warm_up_audio
from .config import Settings
from .hotkey import Combo, InvalidCombo, create_backend
from .output import ChimePlayer, copy, create_paste_backend
from .pipeline.session import (
    DictationSession,
    SessionDeps,
    SessionResult,
    SessionState,
    Stage,
)
from .providers.base import (
    CacheMode,
    Modality,
    ProviderError,
    ReasoningEffort,
    ServiceTier,
    Support,
    TranscribeOptions,
    TransformOptions,
)
from .providers.catalog import ModelCatalog
from .providers.registry import SPECS, build_transcriber, build_transformer
from .ui import enterkey, wheelguard
from .ui.overlay import LiveOverlay
from .ui.settings_window import SettingsWindow
from .ui.theme import apply_theme
from .ui.tray import TrayIcon, ensure_tray_available
from .updater import UpdateService, install_root, mark_healthy, read_current

# The repository the updater watches. Public, so release assets download without credentials.
UPDATE_REPO = "alanmun/yada"
# Parameters worth probing when the provider publishes no capability metadata.
PROBE_PARAMETERS = ["reasoning", "service_tier"]


class AsyncioThread(threading.Thread):
    """Hosts the single event loop that owns all network I/O."""

    def __init__(self) -> None:
        super().__init__(name="yada-async", daemon=True)
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()

    def run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        self.loop.run_forever()

    def wait_ready(self, timeout: float = 5.0) -> bool:
        return self._ready.wait(timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.join(timeout=3.0)


class EventBridge(QObject):
    """Marshals session events from the asyncio thread onto the Qt thread.

    Implements the SessionEvents protocol structurally; the emit calls are thread-safe and
    Qt queues them for the main thread.
    """

    state = Signal(object)
    partial = Signal(str)
    finished = Signal(object)
    error = Signal(str)
    warning = Signal(str)
    update_status = Signal(object)
    catalog_changed = Signal()
    # Requests arriving from the IPC worker thread. Emitting a signal is thread-safe;
    # QTimer.singleShot is not -- it creates the timer in the calling thread, which has no
    # event loop, so the callback would never run.
    open_settings_requested = Signal()
    quit_requested = Signal()
    # Results of a provider key test. Must be a signal: the test runs on the asyncio
    # thread, and touching a QLabel from there is undefined -- in practice the update was
    # simply discarded, so pressing Test appeared to do nothing at all.
    provider_test_result = Signal(str, str)
    # Input level for the settings meter. Emitted from the PortAudio callback
    # thread, which may not touch a widget, so it has to be a signal.
    audio_level = Signal(float)
    # Clipboard write plus the paste keystroke. Also must be a signal, and this one was
    # worse than a discarded update: Qt's Windows clipboard is OLE-based and requires the
    # GUI thread, so calling it from the asyncio thread blocked forever. The text reached
    # the clipboard and the read-back never returned, inside the lock the session holds
    # while finishing -- so the state stayed on "Transcribing…" indefinitely and nothing
    # was ever pasted.
    deliver_requested = Signal(str, object)

    def on_state(self, state: SessionState) -> None:
        self.state.emit(state)

    def on_partial(self, text: str) -> None:
        self.partial.emit(text)

    def on_finished(self, result: SessionResult) -> None:
        self.finished.emit(result)

    def on_error(self, message: str) -> None:
        self.error.emit(message)

    def on_warning(self, message: str) -> None:
        self.warning.emit(message)


class YadaApp(QObject):
    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self.app = app
        self.settings: Settings = config.load()
        self.catalog = ModelCatalog()
        self.bridge = EventBridge()
        self.chimes = ChimePlayer()
        self.paste_backend = create_paste_backend()
        self.settings_window: SettingsWindow | None = None
        self._hotkey = None
        # An AudioCapture opened only while the settings meter is running.
        self._level_capture: AudioCapture | None = None
        self._ipc: ipc.CommandServer | None = None
        self._updates: UpdateService | None = None

        self.async_thread = AsyncioThread()
        self.async_thread.start()
        self.async_thread.wait_ready()
        self.loop = self.async_thread.loop

        self.session = DictationSession(
            self.loop,
            SessionDeps(
                settings=lambda: self.settings,
                transcriber=self._build_transcriber,
                transformer=self._build_transformer,
                events=self.bridge,
                chime=self._chime,
                deliver=self.bridge.deliver_requested.emit,
            ),
        )

        self.tray = TrayIcon(shortcut_label=self._shortcut_label())
        # The only place live transcription is visible. Never takes focus; see overlay.py.
        self.overlay = LiveOverlay()
        self._connect()

    # ==================================================================================
    # Startup
    # ==================================================================================

    def start(self) -> None:
        self._configure_chimes()

        # Materialise defaults on first run, so there is a file to read and hand-edit rather
        # than an absence the user has to guess at.
        if not config.config_path().exists():
            with contextlib.suppress(OSError):
                config.save(self.settings)

        if note := ensure_tray_available():
            # Better a visible complaint than an app that appears not to have started.
            print(f"yada: {note}")

        if not self._start_ipc():
            print("yada is already running; bringing that instance forward instead.")
            self.app.quit()
            return
        self.tray.show()
        self._sync_provider_capabilities()
        self._apply_notification_setting()
        self._start_hotkey()
        # On its own thread rather than the loop: PortAudio initialisation is blocking C
        # code, and the point is to keep it away from anything that is being waited on.
        threading.Thread(target=warm_up_audio, name="yada-audio-warmup", daemon=True).start()

        # Kick discovery and the update check after the UI is up, so neither delays the
        # tray icon appearing.
        QTimer.singleShot(300, lambda: self.refresh_models("transcription"))
        QTimer.singleShot(600, lambda: self.refresh_models("transform"))
        self._start_updates()

        # Tell the launcher this build actually starts, which is what disarms the automatic
        # rollback for the version that is running.
        if version := read_current():
            with contextlib.suppress(Exception):
                mark_healthy(version)

        self._sync_desktop_integration()
        self._tidy_install()

    def _tidy_install(self) -> None:
        """Drop superseded versions and abandoned downloads.

        Pruning used to happen only after an update was staged, so an install that never
        received one kept everything forever -- and each version directory is around
        190 MB. Doing it at startup means disk is reclaimed even if updates are switched
        off entirely.

        Only genuinely stale downloads are removed. Clearing the folder wholesale is not
        safe even at startup: another copy of yada may have a download in flight, and it
        then fails with a bare errno that reads like antivirus interference.
        """
        from .updater import prune_old_versions
        from .updater.github import clear_stale_downloads

        # Only abandoned downloads. Wiping the folder outright destroyed a download that
        # another copy of yada had in flight, which then failed with a bare errno.
        with contextlib.suppress(OSError):
            if dropped := clear_stale_downloads():
                print(f"yada: removed abandoned download(s): {', '.join(dropped)}")
        try:
            if removed := prune_old_versions():
                print(f"yada: removed superseded version(s): {', '.join(removed)}")
        except OSError as exc:
            print(f"yada: could not prune old versions ({exc})")

    def _sync_desktop_integration(self) -> None:
        """Keep the Start Menu shortcut and autostart entry pointing at this version.

        This is the job the old launcher binary used to do by being a fixed path. It was a
        one-file PyInstaller executable, which Defender removed as
        Trojan:Win32/Bearfoos.A!ml along with the shortcut and the run key. Now shortcuts
        point straight at a version's own executable and the running version repoints them,
        which also repairs them if something else removed them.

        Best-effort throughout: none of this is required for yada to work, and failing to
        write a shortcut must never stop the app starting.
        """
        from .relaunch import claim_healthy, running_version_dir

        claim_healthy()

        here = running_version_dir()
        if here is None:
            return  # source checkout: nothing to point at
        executable = here / ("yada.exe" if sys.platform == "win32" else "yada")
        if not executable.exists():
            return

        if sys.platform == "win32":
            self._sync_windows_integration(executable)
        else:
            self._sync_linux_integration(executable)

    def _sync_windows_integration(self, executable: Path) -> None:
        import subprocess
        import winreg

        appdata = os.environ.get("APPDATA")
        if appdata:
            lnk = Path(appdata) / "Microsoft/Windows/Start Menu/Programs/yada.lnk"
            script = (
                f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
                f"$s.TargetPath='{executable}';$s.WorkingDirectory='{executable.parent}';"
                "$s.Description='Press a shortcut, speak, get text';$s.Save()"
            )
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                lnk.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                    check=False,
                    capture_output=True,
                    timeout=30,
                )

        run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with (
            contextlib.suppress(OSError),
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE) as key,
        ):
            if self.settings.start_on_login:
                winreg.SetValueEx(key, "yada", 0, winreg.REG_SZ, f'"{executable}"')
            else:
                with contextlib.suppress(FileNotFoundError, OSError):
                    winreg.DeleteValue(key, "yada")

    def _sync_linux_integration(self, executable: Path) -> None:
        data_home = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
        with contextlib.suppress(OSError):
            bin_dir = Path.home() / ".local" / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            link = bin_dir / "yada"
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(executable)

        desktop = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=yada\n"
            "GenericName=Dictation\n"
            "Comment=Press a shortcut, speak, get text\n"
            f"Exec={executable}\n"
            "Terminal=false\n"
            "Categories=Utility;AudioVideo;\n"
            "StartupNotify=false\n"
        )
        with contextlib.suppress(OSError):
            apps = data_home / "applications"
            apps.mkdir(parents=True, exist_ok=True)
            (apps / "yada.desktop").write_text(desktop, encoding="utf-8")

        autostart = (
            Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "autostart"
        )
        with contextlib.suppress(OSError):
            entry = autostart / "yada.desktop"
            if self.settings.start_on_login:
                autostart.mkdir(parents=True, exist_ok=True)
                # Quiet at login, for the same reason as the Windows run key.
                entry.write_text(
                    desktop.replace(f"Exec={executable}", f"Exec={executable} --minimized"),
                    encoding="utf-8",
                )
            elif entry.exists():
                entry.unlink()

    def _connect(self) -> None:
        self.tray.toggle_requested.connect(self.toggle)
        self.tray.settings_requested.connect(self.show_settings)
        self.tray.copy_last_requested.connect(self._copy_last)
        self.tray.check_updates_requested.connect(self.check_updates)
        self.tray.restart_requested.connect(self.restart)
        self.tray.quit_requested.connect(self.quit)

        self.bridge.state.connect(self.tray.set_state, Qt.ConnectionType.QueuedConnection)
        self.bridge.state.connect(self._on_session_state, Qt.ConnectionType.QueuedConnection)
        self.bridge.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        self.bridge.error.connect(self._on_error, Qt.ConnectionType.QueuedConnection)
        self.bridge.warning.connect(self._on_warning, Qt.ConnectionType.QueuedConnection)
        self.bridge.update_status.connect(
            self._on_update_status, Qt.ConnectionType.QueuedConnection
        )
        self.bridge.catalog_changed.connect(
            self._push_status_to_settings, Qt.ConnectionType.QueuedConnection
        )
        self.bridge.open_settings_requested.connect(
            self.show_settings, Qt.ConnectionType.QueuedConnection
        )
        self.bridge.quit_requested.connect(self.quit, Qt.ConnectionType.QueuedConnection)
        self.bridge.deliver_requested.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        self.bridge.audio_level.connect(self._on_audio_level, Qt.ConnectionType.QueuedConnection)
        self.bridge.partial.connect(self.overlay.set_partial, Qt.ConnectionType.QueuedConnection)
        self.bridge.provider_test_result.connect(
            self._on_provider_test_result, Qt.ConnectionType.QueuedConnection
        )

    def _start_ipc(self) -> bool:
        def handler(command: str, _payload: dict) -> dict:
            if command == "toggle":
                self.session.toggle()
                return {"ok": True, "state": str(self.session.state)}
            if command == "settings":
                self.bridge.open_settings_requested.emit()
                return {"ok": True}
            if command == "quit":
                self.bridge.quit_requested.emit()
                return {"ok": True}
            return {"ok": False, "error": f"unknown command {command!r}"}

        server = ipc.CommandServer(handler)
        try:
            server.start()
        except ipc.AlreadyRunning:
            # __main__ checks this before starting, so reaching here means another instance
            # appeared in between. Previously this just returned, leaving a second copy
            # running with no command socket -- invisible, unstoppable, and holding the
            # microphone. A rival wins; we hand over our intent and leave.
            ipc.send_command("settings")
            return False
        self._ipc = server
        return True

    def _on_session_state(self, state: SessionState) -> None:
        """Drive the overlay, and hand the microphone to the dictation.

        The overlay says "Listening" until a delta arrives and "Transcribing live" once one
        does. A dictation that never shows live text is therefore visibly not live -- a
        distinction that could not be made from outside the app at all before.
        """
        if state is SessionState.RECORDING:
            self.tray.set_problem(None)
            self.overlay.begin()
        elif state is SessionState.TRANSCRIBING:
            self.overlay.set_status("Finishing…")
        if state is not SessionState.IDLE and self._level_capture is not None:
            self._stop_level_capture()
            if self.settings_window is not None:
                self.settings_window.stop_mic_test("Paused while a dictation is running.")

    def _set_mic_test(self, active: bool) -> None:
        """Open or release the microphone for the settings level meter.

        Separate from the recording capture on purpose: two streams on one device is a
        reliable way to get neither, so a dictation always wins and the meter steps aside.
        """
        self._stop_level_capture()
        if not active:
            return
        if self.session.state is not SessionState.IDLE:
            if self.settings_window is not None:
                self.settings_window.stop_mic_test("Not while a dictation is running.")
            return
        capture = AudioCapture(
            self._on_level_frames,
            device=self.settings.audio.device,
            gain=self.settings.audio.input_gain,
        )
        try:
            capture.start()
        except AudioDeviceError as exc:
            if self.settings_window is not None:
                self.settings_window.stop_mic_test(str(exc))
            return
        self._level_capture = capture

    def _stop_level_capture(self) -> None:
        capture, self._level_capture = self._level_capture, None
        if capture is not None:
            with contextlib.suppress(Exception):
                capture.stop()

    def _on_level_frames(self, pcm16: bytes) -> None:
        # PortAudio's callback thread. Emitting is the only safe thing to do from here.
        self.bridge.audio_level.emit(peak_level(pcm16))

    def _on_audio_level(self, level: float) -> None:
        if self.settings_window is not None:
            self.settings_window.set_audio_level(level)

    def _reset_settings(self) -> None:
        """Every setting back to its starting value. Keys are deliberately untouched.

        Routed through the ordinary save handler rather than writing the file directly, so
        the theme, the shortcut, the chimes and the desktop integration are all reapplied
        by the same code that handles any other change -- a reset that left the running app
        on the old shortcut would be worse than not offering one.
        """
        fresh = Settings()
        if self.settings_window is not None:
            # Load first: the handler pushes state back into the window, and the window's
            # own widgets are what the next autosave will read.
            self.settings_window.load(fresh)
        self._on_settings_saved(fresh)

    def _sync_provider_capabilities(self) -> None:
        """Hand providers what earlier runs learned, and persist what this one learns.

        A refused parameter used to be relearned by failing once per launch. The catalog's
        probe store already existed for exactly this answer; a refusal during a real request
        is simply a better measurement than a probe, because it is the call being made.
        """
        from .providers import openai_provider

        openai_provider.seed_unsupported(self.catalog.unsupported_parameters("openai"))
        openai_provider.set_unsupported_sink(
            lambda model, field, detail: self.catalog.record_support(
                "openai", model, field, Support.UNSUPPORTED, detail
            )
        )

    def _apply_notification_setting(self) -> None:
        self.tray.notifications_enabled = self.settings.output.show_notifications
        self.overlay.set_enabled(self.settings.output.show_overlay)

    def _start_hotkey(self) -> None:
        try:
            combo = Combo.parse(self.settings.hotkey.combo)
        except InvalidCombo as exc:
            self.tray.notify("Shortcut problem", str(exc), warning=True)
            combo = Combo.parse("ctrl+shift+;")
        self._hotkey = create_backend(self.settings.hotkey.backend, loop=self.loop)
        self._hotkey.start(combo, self.session.toggle)
        self.tray.set_shortcut_label(self._shortcut_label())

    def _start_updates(self) -> None:
        service = UpdateService(
            repo=UPDATE_REPO,
            current_version=self._current_version(),
            on_change=self.bridge.update_status.emit,
        )
        self._updates = service
        asyncio.run_coroutine_threadsafe(_start_service(service), self.loop)

    @staticmethod
    def _current_version() -> str:
        from . import __version__

        return read_current() or __version__

    def _shortcut_label(self) -> str:
        """What the tray tooltip says about the shortcut, including when it is not live.

        The tooltip is the one place a problem can be reported without interrupting
        anybody, which matters more now that notifications are off by default on Windows.
        A registration that failed used to leave the tooltip advertising the shortcut as
        though it worked.
        """
        try:
            label = Combo.parse(self.settings.hotkey.combo).display
        except InvalidCombo:
            label = self.settings.hotkey.combo
        problem = self._hotkey.problem() if self._hotkey is not None else None
        return f"{label} — not registered ({problem})" if problem else label

    # ==================================================================================
    # Providers
    # ==================================================================================

    def _key_for(self, provider_id: str) -> str | None:
        spec = SPECS.get(provider_id)
        return secrets.get_key(provider_id, spec.env_var if spec else None)

    def _build_transcriber(self):
        provider_id = self.settings.transcription.provider
        key = self._key_for(provider_id)
        if not key:
            return None
        try:
            provider = build_transcriber(provider_id, key)
        except (KeyError, ProviderError):
            return None
        # Seed cached per-model metadata first, so capability answers work offline.
        if hasattr(provider, "seed_models"):
            provider.seed_models(
                self.catalog.entry(provider_id).for_modality(Modality.TRANSCRIPTION)
            )

        model, warning = self.catalog.resolve_model(
            provider_id,
            self.settings.transcription.model,
            Modality.TRANSCRIPTION,
            auto_select_newest=self.settings.transcription.auto_select_newest,
        )
        if warning:
            self.bridge.warning.emit(warning)
        if not model:
            # Discovery has not run yet. A provider-specific fallback is better than
            # refusing to record; the request will fail loudly if it is wrong.
            model = "gpt-live-transcribe" if provider_id == "openai" else ""
        if not model:
            return None

        caps = provider.capabilities()
        vocab = self.settings.vocabulary
        opts = TranscribeOptions(
            model=model,
            keywords=list(vocab.terms) if caps.keywords else (),
            prompt=(vocab.context_prompt or None) if caps.prompt else None,
            languages=list(vocab.languages) if caps.languages else (),
            delay=self.settings.transcription.delay if caps.delay_tuning else None,
        )
        return provider, opts

    def _build_transformer(self):
        provider_id = self.settings.transform.provider
        key = self._key_for(provider_id)
        if not key:
            return None
        try:
            provider = build_transformer(provider_id, key)
        except (KeyError, ProviderError):
            return None
        if hasattr(provider, "seed_models"):
            provider.seed_models(self.catalog.entry(provider_id).for_modality(Modality.TEXT))

        tf = self.settings.transform
        effort = _as_enum(ReasoningEffort, tf.reasoning_effort, ReasoningEffort.NONE)
        tier = _as_enum(ServiceTier, tf.service_tier, ServiceTier.STANDARD)
        caps = provider.capabilities(tf.model)
        if caps.reasoning_effort is Support.UNSUPPORTED:
            effort = ReasoningEffort.NONE
        if caps.priority_processing is Support.UNSUPPORTED:
            tier = ServiceTier.STANDARD

        opts = TransformOptions(
            model=tf.model,
            reasoning_effort=effort,
            service_tier=tier,
            temperature=tf.temperature,
            max_output_tokens=tf.max_output_tokens,
            cache_mode=_as_enum(CacheMode, tf.cache_mode, CacheMode.DISABLED),
        )
        return provider, opts

    # ==================================================================================
    # Actions
    # ==================================================================================

    def toggle(self) -> None:
        self.session.toggle()

    def show_settings(self) -> None:
        if self.settings_window is None:
            window = SettingsWindow(self.settings)
            window.saved.connect(self._on_settings_saved)
            window.refresh_models_requested.connect(self.refresh_models)
            window.key_changed.connect(self._on_key_changed)
            window.test_provider_requested.connect(self._test_provider)
            window.check_updates_requested.connect(self.check_updates)
            window.preview_sound_requested.connect(self._preview_sound)
            window.mic_test_requested.connect(self._set_mic_test)
            window.reset_requested.connect(self._reset_settings)
            window.restart_requested.connect(self.restart)
            self.settings_window = window
        else:
            # Reloading resets every field from settings, so anything still sitting in the
            # autosave debounce has to be written first or it is silently discarded.
            self.settings_window.flush_pending_save()
            self.settings_window.flush_pending_keys()
            self.settings_window.load(self.settings)
        # Shown first on purpose: `_push_status_to_settings` skips a window that is not
        # visible, so pushing before showing meant the model pickers were never populated
        # on the first open -- they sat empty until something else happened to trigger a
        # refresh, which read as "the list is empty, click Refresh".
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()
        self._push_status_to_settings()
        self._refresh_stale_models()

    def check_updates(self) -> None:
        """Show the window on the Updates tab, then run the check.

        Triggered from the tray it used to only schedule the coroutine: nothing opened,
        nothing visibly changed, and there was no way to tell whether it had done anything
        at all. The status line on that tab is the result, so the tab has to be in front of
        the user for the action to mean anything. Harmless when the settings window is
        already open there, which is the other way in.
        """
        self.show_settings()
        if self.settings_window is not None:
            self.settings_window.focus_tab("Updates")
        if self._updates is None:
            return
        asyncio.run_coroutine_threadsafe(self._updates.check_now(), self.loop)

    def refresh_models(self, kind: str) -> None:
        asyncio.run_coroutine_threadsafe(self._refresh_models(kind), self.loop)

    def _on_key_changed(self, _provider_id: str) -> None:
        """A key was entered or cleared: that is the moment discovery becomes possible.

        Both kinds, because one key usually unlocks both transcription and transform for
        the same provider.
        """
        self.refresh_models("transcription")
        self.refresh_models("transform")

    def _refresh_stale_models(self) -> None:
        """Fetch anything the cache does not already have, when settings opens.

        The point of the model list is that it is never hand-maintained, so it should not
        need a button press either. Cached entries appear instantly; a stale or empty one
        is fetched in the background and the picker updates when it lands.
        """
        ttl = self.settings.model_cache_ttl_hours
        for kind, provider_id in (
            ("transcription", self.settings.transcription.provider),
            ("transform", self.settings.transform.provider),
        ):
            entry = self.catalog.entry(provider_id)
            if not entry.models or self.catalog.is_stale(provider_id, ttl):
                self.refresh_models(kind)

    def quit(self) -> None:
        self._shutdown(relaunch=False)

    def restart(self) -> None:
        """Quit and start again, which is what applies a staged update.

        The launcher always picks the newest complete version, so simply starting again
        lands on the downloaded release -- there is no separate install step.
        """
        self._shutdown(relaunch=True)

    def _shutdown(self, *, relaunch: bool) -> None:
        """Shut down, and guarantee the process actually ends.

        Every step is individually suppressed. Previously one failure between releasing
        the command socket and calling app.quit() left the app resident: no tray icon, no
        socket, unreachable and unstoppable, still holding the microphone -- and on Windows
        still holding its own files, which is what makes a reinstall fail. A process found
        in that state had been up for over an hour.

        The watchdog is the belt to that braces. If the Qt event loop has not ended
        shortly after being asked to, the process exits anyway. os._exit skips interpreter
        cleanup, which is exactly what is wanted here: the app's own cleanup has already
        run above, and lingering is worse than an abrupt exit.
        """
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(self.session.shutdown(), self.loop)
        # The meter holds a second stream on the same device; a lingering one is exactly
        # the "still holding the microphone" state described above.
        self._stop_level_capture()
        with contextlib.suppress(Exception):
            self.overlay.dismiss()
        if self._hotkey is not None:
            with contextlib.suppress(Exception):
                self._hotkey.stop()
        if self._updates is not None:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._updates.stop(), self.loop)

        # Order matters. The replacement instance checks whether one is already running,
        # so the socket must be closed before it starts -- otherwise it finds us, treats
        # itself as a second copy, forwards a command and exits, leaving nothing running.
        if self._ipc is not None:
            with contextlib.suppress(Exception):
                self._ipc.stop()
            self._ipc = None

        with contextlib.suppress(Exception):
            self.tray.hide()
        if relaunch:
            with contextlib.suppress(Exception):
                self._spawn_replacement()
        with contextlib.suppress(Exception):
            self.async_thread.stop()

        self._arm_exit_watchdog()
        with contextlib.suppress(Exception):
            self.app.quit()

    def _arm_exit_watchdog(self, *, grace_ms: int = 2500) -> None:
        """Force the process to end if the event loop refuses to.

        Runs on a plain daemon thread rather than a QTimer: if Qt is the thing that is
        stuck, a Qt timer will never fire.
        """
        import os as _os
        import threading as _threading

        def bail() -> None:
            import time

            time.sleep(grace_ms / 1000)
            # Still here means app.exec() did not return. Nothing left to save.
            _os._exit(0)

        _threading.Thread(target=bail, name="yada-exit-watchdog", daemon=True).start()

    def _spawn_replacement(self) -> None:
        """Start a detached copy of ourselves, surviving this process's exit."""
        import subprocess

        launcher = install_root() / ("yada.exe" if sys.platform == "win32" else "yada")
        if launcher.exists():
            command = [str(launcher)]
        elif getattr(sys, "frozen", False):
            command = [sys.executable]
        else:
            # Source checkout: no managed install to launch.
            command = [sys.executable, "-m", "yada"]
        try:
            if sys.platform == "win32":
                # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                subprocess.Popen(command, close_fds=True, creationflags=0x00000008 | 0x00000200)
            else:
                subprocess.Popen(
                    command,
                    close_fds=True,
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            # Nothing useful left to do: the tray is already gone. Recorded for the log.
            print(f"yada: could not restart automatically ({exc})")

    # ==================================================================================
    # Async work
    # ==================================================================================

    async def _refresh_models(self, kind: str) -> None:
        if kind == "capabilities":
            await self._refresh_capabilities()
            return
        if kind == "transcription":
            provider_id = self.settings.transcription.provider
            modality = Modality.TRANSCRIPTION
            built = self._build_transcriber()
            provider = built[0] if built else None
        else:
            provider_id = self.settings.transform.provider
            modality = Modality.TEXT
            built = self._build_transformer()
            provider = built[0] if built else None

        if provider is None:
            # Almost always a missing key. Previously this returned silently, so Refresh
            # appeared to do nothing at all.
            self.bridge.catalog_changed.emit()
            return
        await self.catalog.refresh(provider_id, provider, modality=modality)
        self.bridge.catalog_changed.emit()
        if kind == "transform":
            await self._refresh_capabilities()

    async def _refresh_capabilities(self) -> None:
        """Ask, or measure, whether the selected model honours the optional parameters."""
        built = self._build_transformer()
        if built is None:
            return
        provider, _ = built
        model = self.settings.transform.model
        caps = provider.capabilities(model)

        # Only probe when the provider genuinely cannot say and the user allows it.
        if (
            self.settings.probe_capabilities
            and model
            and hasattr(provider, "probe_parameter")
            and Support.UNKNOWN in (caps.reasoning_effort, caps.priority_processing)
        ):
            await self.catalog.probe(
                self.settings.transform.provider, provider, model, PROBE_PARAMETERS
            )
        self.bridge.catalog_changed.emit()

    # ==================================================================================
    # Slots
    # ==================================================================================

    def _configure_chimes(self) -> None:
        """Point each stage at its selected sound and preload it.

        Called at startup and after every save, so switching a chime takes effect without
        a restart. A selection that no longer resolves falls back to the built-in.
        """
        out = self.settings.output
        self.chimes.configure(
            listening=out.chime_listening_sound,
            transcription=out.chime_transcription_sound,
            transformation=out.chime_transformation_sound,
            volume=out.chime_volume,
        )

    def _preview_sound(self, sound_id: str) -> None:
        """Play a sound from the settings window, at the volume currently on the slider."""
        window = self.settings_window
        if window is not None:
            self.chimes.set_volume(window.chime_volume.value())
        if sound_id:
            self.chimes.preview(sound_id)

    def _chime(self, stage: Stage) -> None:
        out = self.settings.output
        if stage is Stage.LISTENING and not out.chime_on_listening:
            return
        if stage is Stage.TRANSCRIPTION and not out.chime_on_transcription:
            return
        if stage is Stage.TRANSFORMATION and not out.chime_on_transformation:
            return
        self.chimes.play(stage)

    def _deliver(self, text: str, _stage: Stage) -> None:
        """Clipboard first, then the keystroke. Order matters: if pasting fails, the text is
        still on the clipboard and one Ctrl+V away.

        Runs on the Qt thread via `deliver_requested`, never called directly. Both halves
        need it: Qt's clipboard is GUI-thread-only, and SendInput targets the foreground
        window rather than a thread but has no business being called from the event loop
        that is also feeding audio.
        """
        ok, error = copy(text)
        if not ok:
            self.bridge.warning.emit(f"Could not copy to the clipboard: {error}")
            return
        pasted, paste_error = self.paste_backend.paste()
        if not pasted and paste_error:
            self.bridge.warning.emit(f"{paste_error} The text is on your clipboard.")

    def _copy_last(self) -> None:
        if text := self.tray.last_text:
            ok, error = copy(text)
            if not ok:
                self.tray.notify("Copy failed", error or "unknown error", warning=True)

    def _on_finished(self, result: SessionResult) -> None:
        self.tray.set_state(SessionState.IDLE)
        self.tray.set_result(result)
        if self.settings.output.always_copy_to_clipboard and (
            self.settings.output.paste_mode == "off"
        ):
            copy(result.final_text)

        if result.warnings:
            # A warning has to be read, so the panel stays up carrying it.
            self.tray.set_problem(" ".join(result.warnings))
            self.overlay.finish(result.final_text, status=result.warnings[0])
        else:
            # Nothing to report, so the panel goes. The transcription chime has already
            # confirmed it finished; a "Done" line afterwards is a second confirmation of
            # something the user has just heard, and it outstays its welcome on screen.
            # Whether the transcript came from the live socket or an upload is still on the
            # tray tooltip, next to the word count and duration.
            self.overlay.dismiss()
        for warning in result.warnings:
            self.tray.notify("yada", warning, warning=True)

    # Prefix the session uses for "there is no provider configured".
    NOT_CONFIGURED = "NOT_CONFIGURED: "

    def _on_error(self, message: str) -> None:
        self.tray.set_state(SessionState.IDLE)
        self.tray.set_problem(message)
        self.overlay.report(message)
        if message.startswith(self.NOT_CONFIGURED):
            # Show the window rather than a notification nobody will see, and land on the
            # tab that fixes it. Pressing the shortcut with no key set is the most likely
            # first experience, and a silent failure there is the worst possible one.
            self.tray.notify("yada", message[len(self.NOT_CONFIGURED) :], warning=True)
            self.show_settings()
            if self.settings_window is not None:
                self.settings_window.focus_tab("Providers")
            return
        self.tray.notify("yada", message, warning=True)

    def _on_warning(self, message: str) -> None:
        # Three channels, because the toast is off by default on Windows and a warning that
        # reaches nobody is why "live transcription unavailable" looked like yada simply
        # being slow.
        self.tray.set_problem(message)
        self.overlay.report(message)
        self.tray.notify("yada", message, warning=True)

    def _on_update_status(self, status) -> None:
        if status is not None and self._updates is not None:
            self.tray.set_update_ready(status.ready_version)
            if self.settings_window is not None:
                self.settings_window.set_update_ready(status.ready_version)
        self._push_status_to_settings()

    def _push_status_to_settings(self) -> None:
        window = self.settings_window
        if window is None or not window.isVisible():
            return
        stt_id = self.settings.transcription.provider
        stt_entry = self.catalog.entry(stt_id)
        stt_models = stt_entry.for_modality(Modality.TRANSCRIPTION)
        window.stt_model.set_models(
            stt_models,
            current=self.settings.transcription.model,
            recommended=self._recommended(stt_id, Modality.TRANSCRIPTION, stt_models),
        )
        window.stt_model.set_status(self._model_status(stt_id, stt_entry))
        _, drift = self.catalog.resolve_model(
            stt_id, self.settings.transcription.model, Modality.TRANSCRIPTION
        )
        window.stt_model.set_drift_warning(drift)
        window.set_transcription_capabilities(
            delay=self._delay_support(stt_id, stt_entry, self.settings.transcription.model)
        )

        tf_id = self.settings.transform.provider
        tf_entry = self.catalog.entry(tf_id)
        tf_models = tf_entry.for_modality(Modality.TEXT)
        window.tf_model.set_models(
            tf_models,
            current=self.settings.transform.model,
            recommended=self._recommended(tf_id, Modality.TEXT, tf_models),
        )
        window.tf_model.set_status(self._model_status(tf_id, tf_entry))

        built = self._build_transformer()
        if built is not None:
            provider, _ = built
            caps = provider.capabilities(self.settings.transform.model)
            reasoning = tf_entry.support_for(
                self.settings.transform.model, "reasoning", caps.reasoning_effort
            )
            priority = tf_entry.support_for(
                self.settings.transform.model, "service_tier", caps.priority_processing
            )
            window.set_transform_capabilities(
                reasoning=reasoning, efforts=caps.reasoning_efforts, priority=priority
            )

        if self._hotkey is not None:
            window.set_hotkey_status(self._hotkey.status())
        if self._updates is not None:
            window.set_update_status(self._updates.status.summary())
            window.set_update_ready(self._updates.status.ready_version)
        window.refresh_key_status()

    def _delay_support(self, provider_id: str, entry, model: str) -> Support:
        """Whether this model takes the speed dial: what we measured beats what we assumed."""
        built = self._build_transcriber()
        fallback = Support.UNKNOWN
        if built is not None:
            provider, _ = built
            fallback = (
                Support.SUPPORTED if provider.capabilities().delay_tuning else Support.UNSUPPORTED
            )
        if not model:
            return fallback
        return entry.support_for(model, "delay", fallback)

    def _recommended(self, provider_id: str, modality: Modality, models) -> str:
        """The provider's curated pick, if discovery actually returned it."""
        spec = SPECS.get(provider_id)
        if spec is None:
            return ""
        return spec.recommended(modality, [m.id for m in models])

    def _model_status(self, provider_id: str, entry) -> str:
        """What to say under a model picker.

        A missing key is the overwhelmingly common reason for an empty list, and saying
        "not discovered yet" for it is useless -- it reads as a broken Refresh button
        rather than as a missing prerequisite.
        """
        spec = SPECS.get(provider_id)
        label = spec.label if spec else provider_id
        if not self._key_for(provider_id):
            return (
                f"Add your {label} key on the Providers tab — the model list then loads by itself."
            )
        if not entry.models and not entry.last_error:
            return f"Loading the {label} model list…"
        return entry.staleness_note()

    def _on_settings_saved(self, new_settings: Settings) -> None:
        old = self.settings
        self.settings = new_settings
        config.save(new_settings)

        if (
            new_settings.hotkey.combo != old.hotkey.combo
            or new_settings.hotkey.backend != old.hotkey.backend
        ):
            if self._hotkey is not None:
                with contextlib.suppress(Exception):
                    self._hotkey.stop()
            self._start_hotkey()

        self._configure_chimes()
        self._apply_notification_setting()
        if self._level_capture is not None and (
            new_settings.audio.device != old.audio.device
            or new_settings.audio.input_gain != old.audio.input_gain
        ):
            # Reopen so the meter shows the setting being adjusted, which is the entire
            # reason someone has the meter open while touching the gain.
            self._set_mic_test(True)
        if new_settings.theme != old.theme or new_settings.text_scale != old.text_scale:
            apply_theme(self.app, new_settings.theme, new_settings.text_scale)
        if new_settings.start_on_login != old.start_on_login:
            self._sync_desktop_integration()
        self._tidy_install()
        self.tray.set_shortcut_label(self._shortcut_label())
        if new_settings.transcription.provider != old.transcription.provider:
            self.refresh_models("transcription")
        if new_settings.transform.provider != old.transform.provider:
            self.refresh_models("transform")
        self._push_status_to_settings()

    def _on_provider_test_result(self, provider_id: str, message: str) -> None:
        if self.settings_window is not None:
            self.settings_window.set_provider_test_result(provider_id, message)

    def _test_provider(self, provider_id: str) -> None:
        # Keys are debounced, so a key pasted a moment ago may not be stored yet. Write it
        # first, or Test would report "no key set" for a key plainly visible in the field.
        if self.settings_window is not None:
            self.settings_window.flush_pending_keys()
            self.settings_window.set_provider_test_result(provider_id, "Testing…")
        asyncio.run_coroutine_threadsafe(self._do_test_provider(provider_id), self.loop)

    async def _do_test_provider(self, provider_id: str) -> None:
        """Ask the provider for its model list, which is the cheapest real proof of a key.

        Every result goes back through a signal rather than touching the widget from here:
        this coroutine runs on the asyncio thread.
        """
        report = self.bridge.provider_test_result.emit
        spec = SPECS.get(provider_id)
        key = secrets.get_key(provider_id, spec.env_var if spec else None)
        if not key:
            report(provider_id, "No key set — paste one above.")
            return

        counts: list[str] = []
        try:
            if spec and spec.transcribes:
                stt = build_transcriber(provider_id, key)
                counts.append(f"{len(await stt.list_models())} transcription")
            if spec and spec.transforms:
                tf = build_transformer(provider_id, key)
                counts.append(f"{len(await tf.list_models())} transform")
        except ProviderError as exc:
            report(provider_id, f"Failed: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - the message is the point of the test
            report(provider_id, f"Failed: {type(exc).__name__}: {exc}")
            return

        report(provider_id, f"Working — {' and '.join(counts)} models visible.")
        # A successful test has just proven discovery works, so populate the pickers.
        self.refresh_models("transcription")
        self.refresh_models("transform")


async def _start_service(service: UpdateService) -> None:
    service.start()


def _as_enum(enum_cls, value, default):
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        return default


def main(
    argv: list[str] | None = None,
    *,
    start_recording: bool = False,
    minimized: bool = False,
) -> int:
    app = QApplication(argv or [])
    app.setApplicationName("yada")
    # Deliberately no display name: Qt appends it to every window title, which turned
    # "yada settings" into "yada settings - yada".
    app.setDesktopFileName("yada")
    # The fix for the behaviour that prompted this project: without this, closing the
    # settings window terminates the app instead of leaving it in the tray.
    app.setQuitOnLastWindowClosed(False)

    # Before any widget exists, so nothing is built against the platform palette and then
    # recoloured. Read straight from disk: YadaApp has not loaded settings yet.
    startup_settings = config.load()
    apply_theme(app, startup_settings.theme, startup_settings.text_scale)
    # Kept on the app object: Qt does not own event filters, and an unreferenced one is
    # collected and stops working.
    app._yada_wheel_guard = wheelguard.install(app)
    app._yada_enter_guard = enterkey.install(app)

    yada = YadaApp(app)
    yada.start()

    # Open the window unless told otherwise. A launch that produces nothing visible reads
    # as a launch that failed -- which is exactly what happened when the only feedback was
    # a tray icon Windows 11 hides behind the ^ arrow. The login autostart entry passes
    # --minimized, so starting with the machine stays quiet either way.
    if not minimized and not yada.settings.start_minimized:
        QTimer.singleShot(0, yada.show_settings)
    if start_recording:
        # Reached when a desktop shortcut fired `yada toggle` with nothing running. Begin
        # recording once the tray is up, so the keypress is not silently wasted.
        QTimer.singleShot(400, yada.toggle)
    return app.exec()
