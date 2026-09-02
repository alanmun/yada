"""The settings window.

Not part of the normal flow -- yada lives in the tray -- so this is optimised for being
understood on the rare visit rather than for density. Every non-obvious option carries a
one-line explanation underneath, and anything that depends on the session (whether the
shortcut registered, whether pasting is possible, when models were last discovered) reports
its actual state rather than presenting a control that may quietly do nothing.

Closing this window hides it. Only the tray's Quit action exits, which is the specific
behaviour Whispering gets wrong on Windows.
"""

from __future__ import annotations

import dataclasses
import sys

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import secrets
from ..audio import list_input_devices
from ..config import Settings
from ..hotkey import available_backends, toggle_command
from ..output import create_paste_backend, sounds
from ..pipeline.transform import DEFAULT_SYSTEM_PROMPT, default_steps
from ..providers.base import ReasoningEffort, ServiceTier, Support
from ..providers.registry import PLANNED, SPECS
from .languages import label_for as language_label
from .languages import sorted_codes
from .sound_picker import ChimeRow, SoundLibraryEditor, VolumeRow
from .steps_editor import StepsEditor
from .theme import TEXT_SCALE_LABELS, TEXT_SCALES, THEME_LABELS, THEMES
from .widgets import (
    CheckableComboBox,
    ModelPicker,
    PromptEditor,
    StringListEditor,
    SupportCheckBox,
    button_row,
    error_label,
    hint,
    labelled,
)

PASTE_MODES = [
    ("off", "Never paste automatically"),
    ("after_transcription", "Paste as soon as the transcript is ready"),
    ("after_transformation", "Paste after the AI cleanup finishes"),
]


def _short_version() -> str:
    """Just the version number, for the title bar."""
    from .. import __version__
    from ..updater import read_current

    return read_current() or __version__


def _running_version() -> str:
    """What is actually running, which is not always what the source says.

    An installed copy reports the version directory it was launched from; a source
    checkout reports the package version.
    """
    from .. import __version__
    from ..updater import read_current

    installed = read_current()
    if installed and installed != __version__:
        return f"{installed}  (package {__version__})"
    return installed or f"{__version__}  (running from source)"


def _scrollable(inner: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setWidget(inner)
    return area


def _recommendation_lines(spec) -> list[str]:
    """The curated pick per modality, as lines for the provider panel.

    The first candidate is shown rather than the one discovery returned: this panel is
    about the provider, not about a particular model list, and the picker on the Transcribe
    and Transform tabs marks whichever pick is actually available.
    """
    lines = []
    if spec.transcribes and spec.recommended_transcription:
        lines.append(f"    \u2022 Transcription \u2014 {spec.recommended_transcription[0]}")
    if spec.transforms and spec.recommended_transform:
        lines.append(f"    \u2022 Transform \u2014 {spec.recommended_transform[0]}")
    return lines


class SettingsWindow(QWidget):
    saved = Signal(object)  # Settings
    refresh_models_requested = Signal(str)  # "transcription" | "transform"
    key_changed = Signal(str)  # provider id
    test_provider_requested = Signal(str)  # provider id
    check_updates_requested = Signal()
    preview_sound_requested = Signal(str)
    sound_library_changed = Signal()
    restart_requested = Signal()

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Just the name and version. Qt appends applicationDisplayName to window titles,
        # so "yada settings" rendered as "yada settings - yada"; the display name is left
        # unset in app.py so this is the whole title.
        self.setWindowTitle(f"yada v{_short_version()}")
        # Scaled with the text, capped to the screen: at double size the tab bar overflows
        # a fixed 760px window.
        scale = max(1.0, min(2.0, float(getattr(settings, "text_scale", 1.0))))
        width, height = int(720 * (0.5 + 0.6 * scale)), int(600 * (0.55 + 0.45 * scale))
        if screen := QApplication.primaryScreen():
            available = screen.availableGeometry()
            width = min(width, int(available.width() * 0.9))
            height = min(height, int(available.height() * 0.9))
        self.resize(width, height)
        self._settings = dataclasses.replace(settings)
        self._key_fields: dict[str, QLineEdit] = {}
        self._key_test_status: dict[str, QLabel] = {}
        self._key_timers: dict[str, QTimer] = {}
        # True while a field is displaying a stored key rather than something typed.
        self._key_masked: dict[str, bool] = {}

        # Built before the tabs, because the Updates tab places it. Only appears once an
        # update is downloaded and waiting: restarting is the whole install step, so the
        # action shows up exactly when it does something. The tray menu carries it too.
        self.restart_button = QPushButton("Restart to finish updating")
        self.restart_button.clicked.connect(self.restart_requested.emit)
        self.restart_button.setVisible(False)

        self.tabs = QTabWidget()
        self.tabs.addTab(_scrollable(self._build_providers()), "Providers")
        self.tabs.addTab(_scrollable(self._build_transcription()), "Transcribe")
        self.tabs.addTab(_scrollable(self._build_transform()), "Transform")
        self.tabs.addTab(_scrollable(self._build_vocabulary()), "Vocabulary")
        self.tabs.addTab(_scrollable(self._build_shortcut()), "Shortcut")
        self.tabs.addTab(_scrollable(self._build_audio_output()), "Audio && output")
        self.tabs.addTab(_scrollable(self._build_updates()), "Updates")

        # No Save button. Every change is written as it is made, debounced so that typing
        # in a text field does not mean one write per keystroke. The flag suppresses saves
        # while load() is populating widgets, which would otherwise save the values it just
        # read back over themselves.
        self._loading = False
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._commit)

        # No footer. Its only permanent content was a line saying changes save themselves,
        # which is not worth a strip of window, and the tabs can use the height.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.tabs, 1)

        self.load(settings)
        self._wire_autosave()
        self._size_to_content(settings)

    def _size_to_content(self, settings: Settings) -> None:
        """Open wide enough that the tab bar is not clipped.

        Derived from what the tab bar actually asks for rather than from a multiplier: at
        double text size the labels need roughly 1130px, and a guess that happened to work
        at one scale clipped "Audio & output" at another. Capped to the screen, but never
        below the minimum -- if the display genuinely cannot fit it, Qt scrolls the tab bar,
        which beats a window larger than the desktop.
        """
        needed = self.tabs.tabBar().sizeHint().width() + 48
        # Wide enough by default to show every tab, but not *required* to be: forcing a
        # minimum as wide as the tab bar is the same complaint as forcing a minimum as tall
        # as the monitor. Qt scrolls the tab bar when it does not fit.
        self.setMinimumWidth(460)

        # Height is deliberately unconstrained beyond something usable. Every tab lives in
        # a scroll area, so a short window scrolls rather than clipping -- and a window
        # that could not be made shorter than the monitor was unusable next to anything
        # else. Qt would otherwise take the tallest tab's full content as the minimum.
        self.setMinimumHeight(220)

        scale = max(1.0, min(2.0, float(getattr(settings, "text_scale", 1.0))))
        width, height = needed + 40, int(560 * (0.55 + 0.45 * scale))
        if screen := QApplication.primaryScreen():
            available = screen.availableGeometry()
            width = min(width, int(available.width() * 0.92))
            height = min(height, int(available.height() * 0.85))
        self.resize(max(width, self.minimumWidth()), max(height, self.minimumHeight()))

    # ==================================================================================
    # Providers
    # ==================================================================================

    def _build_providers(self) -> QWidget:
        """One provider at a time.

        Every provider used to be stacked on the page at once, so setting up OpenAI meant
        scrolling past OpenRouter's key field and notes. The chooser picks which one you
        are configuring; everything below it belongs to that provider.
        """
        page = QWidget()
        layout = QVBoxLayout(page)

        self.provider_chooser = QComboBox()
        for spec in SPECS.values():
            self.provider_chooser.addItem(spec.label, spec.id)
        layout.addWidget(labelled("Provider", self.provider_chooser))
        layout.addWidget(
            hint(
                "Keys are shared between a source checkout and the installed app, so you "
                "only enter them once. " + secrets.describe_store()
            )
        )
        layout.addWidget(
            hint(
                "This chooses which provider to set up. Which one yada actually uses is on "
                "the Transcribe and Transform tabs, so you can keep more than one key."
            )
        )

        self._provider_pages = QStackedWidget()
        for spec in SPECS.values():
            self._provider_pages.addWidget(self._build_provider_page(spec))
        self.provider_chooser.currentIndexChanged.connect(
            self._provider_pages.setCurrentIndex
        )
        layout.addWidget(self._provider_pages)

        planned = QGroupBox("Not yet available")
        planned_layout = QVBoxLayout(planned)
        planned_layout.addWidget(
            hint(
                "yada's provider layer is designed for these; each is one file plus a registry "
                "entry, with no changes to the recording pipeline:\n"
                + "\n".join(f"    \u2022 {name} \u2014 {what}" for name, what in PLANNED.items())
            )
        )
        layout.addWidget(planned)
        layout.addStretch(1)
        return page

    def _build_provider_page(self, spec) -> QWidget:
        """The key field and everything else specific to one provider."""
        box = QGroupBox(spec.label)
        form = QFormLayout(box)

        field = QLineEdit()
        field.setPlaceholderText("Paste your API key — it saves itself")
        # textEdited, not textChanged: it fires only for user input, so loading a stored
        # key into the field cannot be mistaken for entering a new one and saved back over
        # the real value.
        field.textEdited.connect(lambda _t, pid=spec.id: self._on_key_edited(pid))
        self._key_fields[spec.id] = field

        clear = QPushButton("Clear")
        clear.clicked.connect(lambda _=False, pid=spec.id: self._clear_key(pid))
        test = QPushButton("Test")
        test.setToolTip("Ask the provider for its model list using this key.")
        test.clicked.connect(lambda _=False, pid=spec.id: self.test_provider_requested.emit(pid))

        row = QHBoxLayout()
        row.addWidget(field, 1)
        row.addWidget(test)
        row.addWidget(clear)
        holder = QWidget()
        holder.setLayout(row)

        # A label of its own, used only by Test. It previously shared the key-status label,
        # so a result appeared for a moment and was then overwritten by a refresh -- a
        # flicker with no way to read what it said.
        test_status = hint("")
        # Hidden until there is something to say, so an unused row does not sit as a gap
        # between the key field and the provider's notes.
        test_status.hide()
        self._key_test_status[spec.id] = test_status

        form.addRow(holder)
        form.addRow(test_status)

        capability = []
        if spec.transcribes:
            capability.append("transcription")
        if spec.transforms:
            capability.append("transform")
        form.addRow(hint(f"{spec.notes} Used for: {', '.join(capability)}."))

        if picks := _recommendation_lines(spec):
            form.addRow(hint("Recommended here:\n" + "\n".join(picks)))
        if spec.env_var:
            form.addRow(
                hint(f"{spec.env_var} in the environment overrides whatever is stored here.")
            )

        holder_page = QWidget()
        page_layout = QVBoxLayout(holder_page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.addWidget(box)
        # Otherwise the single child is given the whole page and the group box stretches,
        # leaving a large empty panel under the last line of text.
        page_layout.addStretch(1)
        return holder_page

    def _on_key_edited(self, provider_id: str) -> None:
        """The user typed or pasted. Clear the masked display and debounce a save."""
        if self._key_masked.get(provider_id):
            # They are replacing a stored key. Whatever survived of the mask is not part
            # of the new value, so start clean.
            self._key_masked[provider_id] = False
            field = self._key_fields[provider_id]
            field.blockSignals(True)
            field.setText("")
            field.blockSignals(False)
            return
        self._schedule_key_save(provider_id)

    def _schedule_key_save(self, provider_id: str) -> None:
        """Debounce the write.

        Separate from the settings debounce because API keys do not live in settings.json
        at all -- they go to the OS keyring. Slightly longer, since a key is long enough
        that a paste can arrive as more than one change event.
        """
        if self._loading:
            return
        timer = self._key_timers.get(provider_id)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(700)
            timer.timeout.connect(lambda pid=provider_id: self._commit_key(pid))
            self._key_timers[provider_id] = timer
        timer.start()

    def _commit_key(self, provider_id: str) -> None:
        field = self._key_fields.get(provider_id)
        if field is None or self._key_masked.get(provider_id):
            return
        secrets.set_key(provider_id, field.text())
        if label := self._key_test_status.get(provider_id):
            label.setText("")  # a result for the previous key means nothing now
            label.hide()
        self.key_changed.emit(provider_id)

    def flush_pending_keys(self) -> None:
        for provider_id, timer in self._key_timers.items():
            if timer.isActive():
                timer.stop()
                self._commit_key(provider_id)

    # A stored key is shown masked to this many bullets, however long it really is. A
    # 164-character key rendered literally is a wall of dots that says nothing.
    MASK_LENGTH = 16

    def refresh_key_status(self) -> None:
        """Show any stored key in its own field, masked except the last four characters.

        Replaces a separate "a key is set, last 4: ..." line. The field is where someone
        looks to see whether a key is set, and recognising the last four characters of
        their own key is the fastest way to confirm it is the right one.
        """
        for pid, spec in SPECS.items():
            field = self._key_fields[pid]
            if field.hasFocus():
                continue  # never rewrite a field somebody is typing into
            key, _store = secrets.resolve_key(pid, spec.env_var)
            field.blockSignals(True)
            if key:
                field.setText("\u2022" * self.MASK_LENGTH + key[-4:])
                self._key_masked[pid] = True
            else:
                field.setText("")
                self._key_masked[pid] = False
            field.blockSignals(False)

    def set_provider_test_result(self, provider_id: str, message: str) -> None:
        if label := self._key_test_status.get(provider_id):
            label.setText(message)
            label.setVisible(bool(message))

    # ==================================================================================
    # Transcription
    # ==================================================================================

    def _build_transcription(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.stt_provider = QComboBox()
        for spec in SPECS.values():
            if spec.transcribes:
                self.stt_provider.addItem(spec.label, spec.id)
        self.stt_provider.currentIndexChanged.connect(self._on_stt_provider_changed)
        layout.addWidget(labelled("Provider", self.stt_provider))

        self.stt_model = ModelPicker()
        self.stt_model.refresh_requested.connect(
            lambda: self.refresh_models_requested.emit("transcription")
        )
        layout.addWidget(
            labelled(
                "Model",
                self.stt_model,
                tip=(
                    "Automatic follows the provider's newest recommended model, so yada does "
                    "not go stale when they release something better."
                ),
            )
        )

        self.stt_streaming = QCheckBox("Transcribe while I speak, when the provider supports it")
        layout.addWidget(self.stt_streaming)
        layout.addWidget(
            hint(
                "With streaming, the text is ready the instant you stop talking. Providers "
                "without a live connection (OpenRouter) transcribe after you stop instead — "
                "the recording is always kept locally either way, so nothing is lost if the "
                "connection drops mid-sentence."
            )
        )

        self.stt_delay = QComboBox()
        for value, label in [
            ("minimal", "Minimal — fastest, slightly less accurate"),
            ("low", "Low"),
            ("medium", "Medium — balanced"),
            ("high", "High"),
            ("xhigh", "Maximum — most accurate, slowest"),
        ]:
            self.stt_delay.addItem(label, value)
        layout.addWidget(
            labelled(
                "Speed vs accuracy",
                self.stt_delay,
                tip=(
                    "Only used by providers that expose this dial. Actual timings vary by "
                    "model, so it is worth trying a couple with your own voice."
                ),
            )
        )
        layout.addStretch(1)
        return page

    def _on_stt_provider_changed(self) -> None:
        self.refresh_models_requested.emit("transcription")

    # ==================================================================================
    # Cleanup (transform)
    # ==================================================================================

    def _build_transform(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.tf_enabled = QCheckBox("Enable transforms after each transcribe job completes")
        self.tf_enabled.toggled.connect(self._on_transform_toggled)
        layout.addWidget(self.tf_enabled)
        layout.addWidget(
            hint("A second chime sounds when the cleanup finishes, so the two stages are "
                 "distinguishable without looking.")
        )

        self.tf_body = QWidget()
        body = QVBoxLayout(self.tf_body)
        body.setContentsMargins(0, 6, 0, 0)

        self.tf_provider = QComboBox()
        for spec in SPECS.values():
            if spec.transforms:
                self.tf_provider.addItem(spec.label, spec.id)
        self.tf_provider.currentIndexChanged.connect(
            lambda: self.refresh_models_requested.emit("transform")
        )

        self.tf_model = ModelPicker(allow_auto=False)
        self.tf_model.refresh_requested.connect(
            lambda: self.refresh_models_requested.emit("transform")
        )
        self.tf_model.changed.connect(lambda _: self.refresh_models_requested.emit("capabilities"))

        row = QHBoxLayout()
        row.addWidget(labelled("Provider", self.tf_provider), 1)
        row.addWidget(labelled("Model", self.tf_model), 2)
        holder = QWidget()
        holder.setLayout(row)
        body.addWidget(holder)

        self.tf_priority = SupportCheckBox("priority")
        body.addWidget(self.tf_priority)

        self.tf_reasoning_box = SupportCheckBox("reasoning")
        self.tf_reasoning_box.toggled.connect(self._on_reasoning_toggled)
        self.tf_reasoning = QComboBox()
        reasoning_row = QHBoxLayout()
        reasoning_row.addWidget(self.tf_reasoning_box)
        reasoning_row.addWidget(self.tf_reasoning, 1)
        reasoning_holder = QWidget()
        reasoning_holder.setLayout(reasoning_row)
        body.addWidget(reasoning_holder)

        self.steps = StepsEditor()
        body.addWidget(labelled("Cleanup steps", self.steps), 1)

        reset = QPushButton("Reset to a single default cleanup step")
        reset.clicked.connect(lambda: self.steps.set_steps(default_steps()))
        body.addLayout(button_row(reset))

        layout.addWidget(self.tf_body, 1)
        return page

    def _on_transform_toggled(self, on: bool) -> None:
        self.tf_body.setEnabled(on)
        if on and not self.steps.steps():
            self.steps.set_steps(default_steps())

    def _on_reasoning_toggled(self, on: bool) -> None:
        self.tf_reasoning.setEnabled(on and self.tf_reasoning_box.isEnabled())

    def set_transform_capabilities(
        self, *, reasoning: Support, efforts: tuple[ReasoningEffort, ...], priority: Support
    ) -> None:
        """Called after capability discovery, which may be a live probe."""
        self.tf_priority.set_support(priority)
        self.tf_reasoning_box.set_support(reasoning)
        current = self.tf_reasoning.currentData()
        self.tf_reasoning.clear()
        for effort in efforts or ():
            self.tf_reasoning.addItem(str(effort), str(effort))
        if current:
            index = self.tf_reasoning.findData(current)
            if index >= 0:
                self.tf_reasoning.setCurrentIndex(index)
        self._on_reasoning_toggled(self.tf_reasoning_box.isChecked())

    # ==================================================================================
    # Vocabulary
    # ==================================================================================

    def _build_vocabulary(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            hint(
                "Names, jargon and acronyms that get misheard. These are sent to the "
                "transcription model as literal vocabulary hints where the provider supports "
                "it, which fixes the spelling while it is still listening — far more effective "
                "than correcting it afterwards. They are also added to the Transform prompt as "
                "a second line of defence."
            )
        )
        self.vocab_terms = StringListEditor(placeholder="e.g. Troutwood")
        layout.addWidget(labelled("Terms and their correct spellings", self.vocab_terms), 1)

        self.vocab_context = PromptEditor(
            "You can explain how you typically use yada here, which might make "
            "transcriptions even more accurate. This is optional."
        )
        layout.addWidget(labelled("What do you usually dictate?", self.vocab_context))

        self.vocab_languages = CheckableComboBox(empty_text="Detect automatically")
        layout.addWidget(
            labelled(
                "Expected languages",
                self.vocab_languages,
                tip=(
                    "Tick any you actually speak into yada. Leave everything unticked to "
                    "let the model detect the language itself — only worth setting if you "
                    "mix languages in one recording."
                ),
            )
        )
        layout.addWidget(
            hint(
                "Tip: for a spelling a model keeps getting wrong anyway, add a Find and replace "
                "step on the Transform tab. It runs locally, costs nothing and cannot be ignored."
            )
        )
        return page

    # ==================================================================================
    # Shortcut
    # ==================================================================================

    def _build_shortcut(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.hotkey_field = QLineEdit()
        self.hotkey_field.setPlaceholderText("ctrl+shift+;")
        self.hotkey_field.textChanged.connect(self._validate_hotkey)
        self.hotkey_error = error_label("")
        self.hotkey_error.hide()
        layout.addWidget(
            labelled(
                "Shortcut",
                self.hotkey_field,
                tip="Needs at least one modifier, or it would fire while you type.",
            )
        )
        layout.addWidget(self.hotkey_error)

        # Only what this platform can actually use. Offering "Wayland portal" on Windows
        # is noise, and picking it would silently fall back to something else anyway.
        self.hotkey_backend = QComboBox()
        backend_labels = {
            "win32": "Windows global hotkey",
            "kde_portal": "Ask the desktop (Wayland portal)",
            "external": "The desktop runs a command",
        }
        self.hotkey_backend.addItem("Automatic (recommended)", "auto")
        for value in available_backends():
            self.hotkey_backend.addItem(backend_labels.get(value, value), value)
        layout.addWidget(labelled("How to register it", self.hotkey_backend))

        self.hotkey_status = QLabel("")
        self.hotkey_status.setWordWrap(True)
        self.hotkey_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # Status the user must be able to read and copy: left at full contrast.
        layout.addWidget(labelled("Status", self.hotkey_status))

        # Everything below is about a restriction that only exists on Wayland. On Windows
        # RegisterHotKey just works, so none of it is shown there.
        if sys.platform != "win32":
            self.copy_command = QPushButton("Copy the command to bind")
            self.copy_command.clicked.connect(self._copy_toggle_command)
            layout.addLayout(button_row(self.copy_command))
            layout.addWidget(
                hint(
                    "On Wayland, applications are not allowed to grab keys. yada asks the "
                    "desktop to own the shortcut; if that is unavailable, bind the command "
                    "above in System Settings → Shortcuts and it will reach yada just as "
                    "fast."
                )
            )
        else:
            layout.addWidget(
                hint(
                    "Registered with Windows directly, so it works from any application. "
                    "If another program has already claimed the combination, the status "
                    "above will say so."
                )
            )
        layout.addStretch(1)
        return page

    def _validate_hotkey(self, text: str) -> None:
        """Report a bad shortcut inline and keep the last good one.

        With autosave, a half-typed "ctrl+shift+" would otherwise be saved and applied on
        every keystroke, and each failure would raise a tray notification.
        """
        from ..hotkey import Combo, InvalidCombo

        try:
            Combo.parse(text)
        except InvalidCombo as exc:
            self.hotkey_error.setText(str(exc))
            self.hotkey_error.show()
            return
        self._last_valid_combo = text.strip()
        self.hotkey_error.hide()

    def _copy_toggle_command(self) -> None:
        from ..output import copy

        ok, error = copy(toggle_command())
        self.hotkey_status.setText(
            f"Copied: {toggle_command()}" if ok else f"Could not copy: {error}"
        )

    def set_hotkey_status(self, text: str) -> None:
        self.hotkey_status.setText(text)

    # ==================================================================================
    # Audio and output
    # ==================================================================================

    def _build_audio_output(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        appearance = QGroupBox("Appearance")
        appearance_layout = QVBoxLayout(appearance)
        self.theme_combo = QComboBox()
        for value in THEMES:
            self.theme_combo.addItem(THEME_LABELS[value], value)
        appearance_layout.addWidget(self.theme_combo)
        appearance_layout.addWidget(
            hint("Applies immediately. 'Match my desktop' uses your system light or dark theme.")
        )
        self.text_scale = QComboBox()
        for value in TEXT_SCALES:
            self.text_scale.addItem(TEXT_SCALE_LABELS[value], value)
        appearance_layout.addWidget(labelled("Text size", self.text_scale))
        appearance_layout.addWidget(
            hint(
                "Scales every label, button and field together, relative to your system's "
                "own UI font. Applies immediately."
            )
        )

        self.start_on_login = QCheckBox("Start yada when I log in")
        appearance_layout.addWidget(self.start_on_login)
        self.start_minimized = QCheckBox("Start minimized to the system tray")
        appearance_layout.addWidget(self.start_minimized)
        appearance_layout.addWidget(
            hint(
                "A dictation shortcut is only useful if yada is already running. Registered "
                "by yada itself rather than by the installer, which keeps antivirus "
                "heuristics calmer about a freshly installed program.\n\n"
                "Starting minimized hides this window on launch. Logging in is always quiet "
                "either way — a window appearing at every boot is nobody's intent."
            )
        )
        layout.addWidget(appearance)

        self.audio_device = QComboBox()
        self._reload_devices()
        rescan = QPushButton("Rescan")
        rescan.clicked.connect(self._reload_devices)
        device_row = QHBoxLayout()
        device_row.addWidget(self.audio_device, 1)
        device_row.addWidget(rescan)
        device_holder = QWidget()
        device_holder.setLayout(device_row)
        layout.addWidget(
            labelled(
                "Microphone",
                device_holder,
                tip=(
                    "Stored by name, not by position, so unplugging a headset will not silently "
                    "switch you to the wrong microphone."
                ),
            )
        )

        self.audio_gain = QDoubleSpinBox()
        self.audio_gain.setRange(0.1, 4.0)
        self.audio_gain.setSingleStep(0.1)
        self.audio_gain.setDecimals(1)
        layout.addWidget(
            labelled(
                "Input gain",
                self.audio_gain,
                tip=(
                    "Only raise this if your microphone is genuinely quiet; clipping hurts "
                    "accuracy."
                ),
            )
        )

        paste_box = QGroupBox("Pasting")
        paste_layout = QVBoxLayout(paste_box)
        self.paste_mode = QComboBox()
        for value, label in PASTE_MODES:
            self.paste_mode.addItem(label, value)
        paste_layout.addWidget(self.paste_mode)
        self.always_copy = QCheckBox("Always copy the result to the clipboard")
        paste_layout.addWidget(self.always_copy)
        backend = create_paste_backend()
        paste_layout.addWidget(hint(backend.describe()))
        layout.addWidget(paste_box)

        notice_box = QGroupBox("Notifications")
        notice_layout = QVBoxLayout(notice_box)
        self.show_notifications = QCheckBox("Show desktop notifications for problems")
        notice_layout.addWidget(self.show_notifications)
        notice_layout.addWidget(
            hint(
                "Off by default on Windows, where these arrive as toasts in the corner of "
                "the screen. Warnings and errors still appear on the tray icon's tooltip "
                "and in this window either way."
            )
        )
        layout.addWidget(notice_box)

        chime_box = QGroupBox("Chimes")
        chime_layout = QVBoxLayout(chime_box)

        self.chime_listening = ChimeRow("Chime when yada starts listening")
        self.chime_transcription = ChimeRow("Chime when the transcript is ready")
        self.chime_transformation = ChimeRow("Chime when the AI cleanup finishes")
        for row in (self.chime_listening, self.chime_transcription, self.chime_transformation):
            row.preview_requested.connect(self.preview_sound_requested.emit)
            chime_layout.addWidget(row)

        self.chime_volume = VolumeRow()
        self.chime_volume.changed.connect(
            lambda: self.preview_sound_requested.emit(self.chime_listening.current_sound())
        )
        chime_layout.addWidget(self.chime_volume)
        chime_layout.addWidget(
            hint(
                "The three built-in sounds differ in shape, not just pitch — a single tap "
                "when listening starts, a rising pair when the transcript lands, a falling "
                "pair when the cleanup finishes — so the stages are distinguishable without "
                "looking. Worth keeping that distinction if you use your own."
            )
        )

        self.sound_library = SoundLibraryEditor()
        self.sound_library.preview_requested.connect(self.preview_sound_requested.emit)
        self.sound_library.library_changed.connect(self._on_library_changed)
        chime_layout.addWidget(labelled("Your own sounds", self.sound_library))
        layout.addWidget(chime_box)
        layout.addStretch(1)
        return page

    def _on_library_changed(self) -> None:
        """Keep both pickers in step with the library after an import or removal."""
        self._reload_sound_pickers(
            self.chime_listening.current_sound(),
            self.chime_transcription.current_sound(),
            self.chime_transformation.current_sound(),
        )
        self.sound_library_changed.emit()

    def _reload_sound_pickers(
        self, listening: str, transcription: str, transformation: str
    ) -> None:
        library = sounds.library()
        self.chime_listening.set_library(library, current=listening)
        self.chime_transcription.set_library(library, current=transcription)
        self.chime_transformation.set_library(library, current=transformation)

    def _reload_devices(self) -> None:
        current = self.audio_device.currentData()
        self.audio_device.clear()
        self.audio_device.addItem("System default", None)
        for dev in list_input_devices():
            suffix = "  (default)" if dev.is_default else ""
            self.audio_device.addItem(f"{dev.name}{suffix}", dev.name)
        if current:
            index = self.audio_device.findData(current)
            if index < 0:
                # Keep a configured-but-absent device visible rather than silently
                # reassigning the user's choice.
                self.audio_device.addItem(f"{current}  (not connected)", current)
                index = self.audio_device.count() - 1
            self.audio_device.setCurrentIndex(index)

    # ==================================================================================
    # Updates
    # ==================================================================================

    def _build_updates(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        from . import __name__ as _  # noqa: F401  (keeps the import block tidy)

        self.current_version_label = QLabel("")
        layout.addWidget(labelled("Installed version", self.current_version_label))

        self.update_enabled = QCheckBox("Keep yada up to date automatically")
        layout.addWidget(self.update_enabled)
        layout.addWidget(
            hint(
                "New versions download and unpack quietly in the background while you work. "
                "The next time yada starts it is already the new version — there is no "
                "installer to sit through. Each release is checked against a signature before "
                "it is ever run, and the previous version is kept so a bad release can be "
                "rolled back."
            )
        )
        self.update_status = QLabel("No update check yet.")
        self.update_status.setWordWrap(True)
        layout.addWidget(labelled("Status", self.update_status))
        check = QPushButton("Check now")
        check.clicked.connect(self.check_updates_requested.emit)
        layout.addLayout(button_row(check, self.restart_button))
        layout.addStretch(1)
        return page

    def set_update_status(self, text: str) -> None:
        self.update_status.setText(text)

    def focus_tab(self, title: str) -> bool:
        """Select a tab by its label. Returns False if there is no such tab.

        By label rather than index: the tab order has already changed twice, and a
        hardcoded index silently selects the wrong page when it changes again.
        """
        for index in range(self.tabs.count()):
            # Tab labels carry Qt mnemonic escaping, e.g. "Audio && output".
            if self.tabs.tabText(index).replace("&&", "&") == title:
                self.tabs.setCurrentIndex(index)
                return True
        return False

    # ==================================================================================
    # Load / collect
    # ==================================================================================

    def load(self, settings: Settings) -> None:
        self._loading = True
        try:
            self._load(settings)
        finally:
            self._loading = False

    def _load(self, settings: Settings) -> None:
        self._settings = dataclasses.replace(settings)
        s = self._settings
        self._last_valid_combo = s.hotkey.combo

        self._select(self.stt_provider, s.transcription.provider)
        self.stt_streaming.setChecked(s.transcription.prefer_streaming)
        self._select(self.stt_delay, s.transcription.delay)

        self.tf_enabled.setChecked(s.transform.enabled)
        self.tf_body.setEnabled(s.transform.enabled)
        self._select(self.tf_provider, s.transform.provider)
        self.tf_priority.setChecked(s.transform.service_tier != str(ServiceTier.STANDARD))
        self.tf_reasoning_box.setChecked(s.transform.reasoning_effort != str(ReasoningEffort.NONE))
        self.steps.set_steps(s.transform.steps or [])

        self.vocab_terms.set_values(s.vocabulary.terms)
        self.vocab_context.setPlainText(s.vocabulary.context_prompt)
        self.vocab_languages.set_options(
            [(code, language_label(code)) for code in sorted_codes(s.vocabulary.languages)],
            checked=s.vocabulary.languages,
        )

        self.hotkey_field.setText(s.hotkey.combo)
        self._select(self.hotkey_backend, s.hotkey.backend)

        self._reload_devices()
        self._select(self.audio_device, s.audio.device)
        self.audio_gain.setValue(s.audio.input_gain)

        self._select(self.theme_combo, s.theme)
        self.start_on_login.setChecked(s.start_on_login)
        self.start_minimized.setChecked(s.start_minimized)
        self._select(self.text_scale, s.text_scale)
        self.update_enabled.setChecked(s.updates_enabled)
        self.current_version_label.setText(_running_version())
        self._select(self.paste_mode, s.output.paste_mode)
        self.stt_model.set_current(s.transcription.model)
        self.tf_model.set_current(s.transform.model)
        self.always_copy.setChecked(s.output.always_copy_to_clipboard)
        self.show_notifications.setChecked(s.output.show_notifications)
        self.chime_listening.set_enabled_state(s.output.chime_on_listening)
        self.chime_transcription.set_enabled_state(s.output.chime_on_transcription)
        self.chime_transformation.set_enabled_state(s.output.chime_on_transformation)
        self._reload_sound_pickers(
            s.output.chime_listening_sound,
            s.output.chime_transcription_sound,
            s.output.chime_transformation_sound,
        )
        self.chime_volume.set_value(s.output.chime_volume)
        self.sound_library.refresh()

        self.refresh_key_status()

    def collect(self) -> Settings:
        s = dataclasses.replace(self._settings)
        s.transcription = dataclasses.replace(s.transcription)
        s.transform = dataclasses.replace(s.transform)
        s.vocabulary = dataclasses.replace(s.vocabulary)
        s.hotkey = dataclasses.replace(s.hotkey)
        s.audio = dataclasses.replace(s.audio)
        s.output = dataclasses.replace(s.output)

        s.transcription.provider = self.stt_provider.currentData() or "openai"
        s.transcription.model = self.stt_model.current_model()
        s.transcription.prefer_streaming = self.stt_streaming.isChecked()
        s.transcription.delay = self.stt_delay.currentData() or "minimal"

        s.transform.enabled = self.tf_enabled.isChecked()
        s.transform.provider = self.tf_provider.currentData() or "openai"
        s.transform.model = self.tf_model.current_model()
        s.transform.service_tier = str(
            ServiceTier.FAST if self.tf_priority.isChecked() else ServiceTier.STANDARD
        )
        s.transform.reasoning_effort = (
            str(self.tf_reasoning.currentData() or ReasoningEffort.LOW)
            if self.tf_reasoning_box.isChecked()
            else str(ReasoningEffort.NONE)
        )
        s.transform.steps = self.steps.steps()

        s.vocabulary.terms = self.vocab_terms.values()
        s.vocabulary.context_prompt = self.vocab_context.toPlainText().strip()
        # An empty list is meaningful: it means "detect automatically", which providers
        # accept. Forcing a default of English would silently override that choice.
        s.vocabulary.languages = self.vocab_languages.checked_values()

        # Never persist a shortcut that does not parse; keep the last one that did.
        s.hotkey.combo = self._last_valid_combo or "ctrl+shift+;"
        s.hotkey.backend = self.hotkey_backend.currentData() or "auto"

        s.audio.device = self.audio_device.currentData()
        s.audio.input_gain = float(self.audio_gain.value())

        s.theme = self.theme_combo.currentData() or "blue"
        s.start_on_login = self.start_on_login.isChecked()
        s.start_minimized = self.start_minimized.isChecked()
        s.text_scale = float(self.text_scale.currentData() or 2.0)
        s.updates_enabled = self.update_enabled.isChecked()
        s.output.paste_mode = self.paste_mode.currentData() or "off"
        s.output.always_copy_to_clipboard = self.always_copy.isChecked()
        s.output.show_notifications = self.show_notifications.isChecked()
        s.output.chime_on_listening = self.chime_listening.is_enabled()
        s.output.chime_on_transcription = self.chime_transcription.is_enabled()
        s.output.chime_on_transformation = self.chime_transformation.is_enabled()
        s.output.chime_listening_sound = (
            self.chime_listening.current_sound() or s.output.chime_listening_sound
        )
        s.output.chime_transcription_sound = (
            self.chime_transcription.current_sound() or s.output.chime_transcription_sound
        )
        s.output.chime_transformation_sound = (
            self.chime_transformation.current_sound() or s.output.chime_transformation_sound
        )
        s.output.chime_volume = self.chime_volume.value()
        return s

    @staticmethod
    def _select(combo: QComboBox, value) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    # ==================================================================================
    # Autosave
    # ==================================================================================

    def _wire_autosave(self) -> None:
        """Connect every input's change signal to the debounced save.

        Dispatched by widget type rather than by trying every signal name on every widget:
        a QComboBox has both currentIndexChanged and editTextChanged, and connecting both
        would save twice for one change.
        """
        from PySide6.QtWidgets import QAbstractSlider, QScrollBar, QSpinBox

        from .sound_picker import ChimeRow, SoundLibraryEditor, VolumeRow
        from .widgets import ModelPicker, StringListEditor

        for child in self.findChildren(QWidget):
            # A scroll bar is a QAbstractSlider, so without this every scroll of the
            # settings page queued a save.
            if isinstance(child, QScrollBar):
                continue
            # API key fields have their own debounce and do not live in settings.json.
            if child in self._key_fields.values():
                continue
            # The provider chooser picks which panel is on screen. It is navigation, not a
            # setting, and saving on it would write a file for a click that changed nothing.
            if child is getattr(self, "provider_chooser", None):
                continue
            if isinstance(child, ChimeRow | VolumeRow | StringListEditor | ModelPicker):
                child.changed.connect(self._schedule_save)
            elif isinstance(child, SoundLibraryEditor):
                child.library_changed.connect(self._schedule_save)
            elif isinstance(child, StepsEditor):
                child.changed.connect(self._schedule_save)
            elif isinstance(child, QCheckBox):
                child.toggled.connect(self._schedule_save)
            elif isinstance(child, QComboBox):
                child.currentIndexChanged.connect(self._schedule_save)
                if child.isEditable():
                    child.editTextChanged.connect(self._schedule_save)
            elif isinstance(child, QLineEdit | PromptEditor):
                child.textChanged.connect(self._schedule_save)
            elif isinstance(child, QDoubleSpinBox | QSpinBox | QAbstractSlider):
                child.valueChanged.connect(self._schedule_save)

    def _schedule_save(self, *_args) -> None:
        if self._loading:
            return
        self._save_timer.start()

    def _commit(self) -> None:
        self.saved.emit(self.collect())

    def flush_pending_save(self) -> None:
        """Write immediately, for the moment the window closes."""
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._commit()

    def set_update_ready(self, version: str | None) -> None:
        self.restart_button.setVisible(bool(version))
        if version:
            self.restart_button.setText(f"Restart to finish updating to {version}")

    # -- window behaviour ----------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:  # Qt naming convention
        """Hide, never quit.

        The absence of exactly this is why Whispering exits when you close its window on
        Windows instead of staying in the tray.
        """
        # Anything typed in the last few hundred milliseconds must not be lost to the
        # debounce simply because the window was closed promptly.
        self.flush_pending_save()
        self.flush_pending_keys()
        event.ignore()
        self.hide()


DEFAULT_PROMPT_HINT = DEFAULT_SYSTEM_PROMPT
