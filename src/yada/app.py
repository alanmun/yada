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
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication

from . import config, ipc, secrets
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
from .ui.settings_window import SettingsWindow
from .ui.tray import TrayIcon, ensure_tray_available
from .updater import UpdateService, mark_healthy, read_current

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
                deliver=self._deliver,
            ),
        )

        self.tray = TrayIcon(shortcut_label=self._shortcut_label())
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
        self.tray.show()

        self._start_ipc()
        self._start_hotkey()

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

    def _connect(self) -> None:
        self.tray.toggle_requested.connect(self.toggle)
        self.tray.settings_requested.connect(self.show_settings)
        self.tray.copy_last_requested.connect(self._copy_last)
        self.tray.check_updates_requested.connect(self.check_updates)
        self.tray.quit_requested.connect(self.quit)

        self.bridge.state.connect(self.tray.set_state, Qt.ConnectionType.QueuedConnection)
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

    def _start_ipc(self) -> None:
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
            # __main__ already checks this; reaching here means a race. Nothing to do.
            return
        self._ipc = server

    def _start_hotkey(self) -> None:
        try:
            combo = Combo.parse(self.settings.hotkey.combo)
        except InvalidCombo as exc:
            self.tray.notify("Shortcut problem", str(exc), warning=True)
            combo = Combo.parse("ctrl+shift+;")
        self._hotkey = create_backend(self.settings.hotkey.backend, loop=self.loop)
        self._hotkey.start(combo, self.session.toggle)
        self.tray.set_shortcut_label(combo.display)

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
        try:
            return Combo.parse(self.settings.hotkey.combo).display
        except InvalidCombo:
            return self.settings.hotkey.combo

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
            window.key_changed.connect(lambda _pid: self.refresh_models("transcription"))
            window.test_provider_requested.connect(self._test_provider)
            window.check_updates_requested.connect(self.check_updates)
            window.preview_sound_requested.connect(self._preview_sound)
            self.settings_window = window
        else:
            self.settings_window.load(self.settings)
        self._push_status_to_settings()
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()

    def check_updates(self) -> None:
        if self._updates is None:
            return
        asyncio.run_coroutine_threadsafe(self._updates.check_now(), self.loop)

    def refresh_models(self, kind: str) -> None:
        asyncio.run_coroutine_threadsafe(self._refresh_models(kind), self.loop)

    def quit(self) -> None:
        asyncio.run_coroutine_threadsafe(self.session.shutdown(), self.loop)
        if self._hotkey is not None:
            with contextlib.suppress(Exception):
                self._hotkey.stop()
        if self._ipc is not None:
            self._ipc.stop()
        if self._updates is not None:
            asyncio.run_coroutine_threadsafe(self._updates.stop(), self.loop)
        self.tray.hide()
        self.async_thread.stop()
        self.app.quit()

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
        if stage is Stage.TRANSCRIPTION and not out.chime_on_transcription:
            return
        if stage is Stage.TRANSFORMATION and not out.chime_on_transformation:
            return
        self.chimes.play(stage)

    def _deliver(self, text: str, _stage: Stage) -> None:
        """Clipboard first, then the keystroke. Order matters: if pasting fails, the text is
        still on the clipboard and one Ctrl+V away."""
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
        for warning in result.warnings:
            self.tray.notify("yada", warning, warning=True)

    def _on_error(self, message: str) -> None:
        self.tray.set_state(SessionState.IDLE)
        self.tray.notify("yada", message, warning=True)

    def _on_warning(self, message: str) -> None:
        self.tray.notify("yada", message, warning=True)

    def _on_update_status(self, status) -> None:
        if status is not None and self._updates is not None:
            self.tray.set_update_ready(status.ready_version)
        self._push_status_to_settings()

    def _push_status_to_settings(self) -> None:
        window = self.settings_window
        if window is None or not window.isVisible():
            return
        stt_id = self.settings.transcription.provider
        stt_entry = self.catalog.entry(stt_id)
        window.stt_model.set_models(
            stt_entry.for_modality(Modality.TRANSCRIPTION),
            current=self.settings.transcription.model,
        )
        window.stt_model.set_status(stt_entry.staleness_note())
        _, drift = self.catalog.resolve_model(
            stt_id, self.settings.transcription.model, Modality.TRANSCRIPTION
        )
        window.stt_model.set_drift_warning(drift)

        tf_id = self.settings.transform.provider
        tf_entry = self.catalog.entry(tf_id)
        window.tf_model.set_models(
            tf_entry.for_modality(Modality.TEXT), current=self.settings.transform.model
        )
        window.tf_model.set_status(tf_entry.staleness_note())

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
        window.refresh_key_status()

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
        self.tray.set_shortcut_label(self._shortcut_label())
        if new_settings.transcription.provider != old.transcription.provider:
            self.refresh_models("transcription")
        if new_settings.transform.provider != old.transform.provider:
            self.refresh_models("transform")
        self._push_status_to_settings()

    def _test_provider(self, provider_id: str) -> None:
        asyncio.run_coroutine_threadsafe(self._do_test_provider(provider_id), self.loop)

    async def _do_test_provider(self, provider_id: str) -> None:
        spec = SPECS.get(provider_id)
        key = secrets.get_key(provider_id, spec.env_var if spec else None)
        window = self.settings_window
        if not key:
            if window:
                window.set_provider_test_result(provider_id, "No key set.")
            return
        try:
            provider = build_transcriber(provider_id, key) if spec and spec.transcribes else (
                build_transformer(provider_id, key)
            )
            models = await provider.list_models()
        except Exception as exc:  # noqa: BLE001 - the message is the point of the test
            if window:
                window.set_provider_test_result(provider_id, f"Failed: {exc}")
            return
        if window:
            window.set_provider_test_result(
                provider_id, f"Working — {len(models)} models visible with this key."
            )


async def _start_service(service: UpdateService) -> None:
    service.start()


def _as_enum(enum_cls, value, default):
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        return default


def main(argv: list[str] | None = None, *, start_recording: bool = False) -> int:
    app = QApplication(argv or [])
    app.setApplicationName("yada")
    app.setApplicationDisplayName("yada")
    app.setDesktopFileName("yada")
    # The fix for the behaviour that prompted this project: without this, closing the
    # settings window terminates the app instead of leaving it in the tray.
    app.setQuitOnLastWindowClosed(False)

    yada = YadaApp(app)
    yada.start()
    if start_recording:
        # Reached when a desktop shortcut fired `yada toggle` with nothing running. Begin
        # recording once the tray is up, so the keypress is not silently wasted.
        QTimer.singleShot(400, yada.toggle)
    return app.exec()
