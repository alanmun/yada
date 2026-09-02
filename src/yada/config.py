"""Settings: dataclasses in, JSON on disk out.

Plain JSON under platformdirs so it is greppable, diffable and hand-editable when the UI
gets in the way. `version` is present from day one so migrations never have to guess what
shape they are reading.

API keys are deliberately absent -- see secrets.py.
"""

from __future__ import annotations

import dataclasses
import json
import types
import typing
from dataclasses import dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, TypeVar

from platformdirs import user_cache_dir, user_config_dir

SCHEMA_VERSION = 1

# 24 kHz mono is what OpenAI's realtime transcription expects, so it is the internal
# canonical rate. Batch providers are fed the same buffer; none of them care.
TARGET_SAMPLE_RATE = 24_000


def config_dir() -> Path:
    return Path(user_config_dir("yada", appauthor=False, roaming=True))


def config_path() -> Path:
    return config_dir() / "settings.json"


def cache_dir() -> Path:
    """Discovered models and capability probes. Safe to delete; rebuilt on next launch."""
    return Path(user_cache_dir("yada", appauthor=False))


# --------------------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Vocabulary:
    """Uncommon terms and their correct spellings -- the single source of truth.

    Fanned out three ways at request time: native `keywords` on providers that support it,
    the transform system prompt, and (optionally) deterministic find_replace steps.
    """

    terms: list[str] = field(default_factory=list)
    # Free-form context about what is usually being dictated. Distinct from `terms`:
    # providers treat keywords as literal strings and the prompt as description.
    context_prompt: str = ""
    languages: list[str] = field(default_factory=lambda: ["en"])


@dataclass(slots=True)
class TranscriptionSettings:
    provider: str = "openai"
    # Empty means "use the highest-ranked discovered model", which keeps the app current
    # without an update. A pinned value here always wins.
    model: str = ""
    prefer_streaming: bool = True
    # When True, an empty `model` resolves to the highest-ranked model discovered live, so
    # the app follows provider releases without an update. Set False to hard-pin.
    auto_select_newest: bool = True
    # minimal|low|medium|high|xhigh where supported. Docs warn actual latency varies by
    # configuration, so this is a dial to benchmark, not a promise.
    delay: str = "minimal"


@dataclass(slots=True)
class TransformStep:
    """One step in an ordered pipeline.

    `find_replace` runs locally and costs nothing, which makes it the right tool for
    recurring misspellings a model keeps reintroducing. `prompt_transform` is the LLM pass.
    """

    type: str = "prompt_transform"  # prompt_transform | find_replace
    enabled: bool = True
    # prompt_transform
    system_prompt: str = ""
    user_prompt_template: str = "{{input}}"
    # find_replace
    find: str = ""
    replace: str = ""
    use_regex: bool = False


@dataclass(slots=True)
class TransformSettings:
    enabled: bool = False
    provider: str = "openai"
    # GPT-5.6 Luna: the efficient tier of the Sol/Terra/Luna family, and the right default
    # for short, high-volume cleanup passes.
    model: str = "gpt-5.6-luna"
    # none|low|medium|high|xhigh|max. Cleanup rarely benefits from reasoning; default off.
    reasoning_effort: str = "none"
    # 'fast' is OpenAI's renamed priority processing: 2x price, up to ~2.5x faster. On a
    # few hundred tokens that is a rounding error, so it is on by default.
    service_tier: str = "fast"
    temperature: float | None = None
    max_output_tokens: int | None = None
    # See CacheMode in providers/base.py for the full cost reasoning. Not in the UI, but
    # editable here so the 1,024-token / 30m-TTL tradeoff can be revisited cheaply.
    cache_mode: str = "disabled"
    steps: list[TransformStep] = field(default_factory=list)


@dataclass(slots=True)
class OutputSettings:
    # off | after_transcription | after_transformation. Never automatic without opt-in.
    paste_mode: str = "off"
    # Two distinct chimes; the transform one is noise when no transform is configured.
    chime_on_transcription: bool = True
    chime_on_transformation: bool = True
    # Which sound each stage uses, as a library id rather than a path: built-in sounds live
    # inside the versioned install directory, which is replaced on every update, so a
    # stored path would break on the next release. See output/sounds.py.
    chime_transcription_sound: str = "builtin:transcription"
    chime_transformation_sound: str = "builtin:transformation"
    chime_volume: float = 0.6
    always_copy_to_clipboard: bool = True


@dataclass(slots=True)
class HotkeySettings:
    combo: str = "ctrl+shift+;"
    # auto resolves to win32 on Windows, kde_portal on Wayland, external as the fallback
    # where the DE owns the binding and invokes `yada toggle`.
    backend: str = "auto"


@dataclass(slots=True)
class AudioSettings:
    # None means the system default input device.
    device: str | None = None
    input_gain: float = 1.0


@dataclass(slots=True)
class Settings:
    version: int = SCHEMA_VERSION
    # "blue" is yada's own palette; "system" matches the desktop. Qt's platform default is
    # a lot of flat grey on Windows, which is why blue is the default rather than an option
    # nobody finds.
    theme: str = "blue"
    # Applied by the running app rather than by the installer. Writing an autostart key
    # from a binary created seconds earlier is part of what got the old launcher
    # quarantined by Defender, so this is yada's own setting to honour.
    start_on_login: bool = True
    # Multiplier on the platform's own UI font, 1.0-2.0. The platform default is 9pt on
    # Windows, which reads as tiny in a window full of prose; 1.6 is a fifth below double,
    # which is where it stopped feeling oversized.
    text_scale: float = 1.6
    transcription: TranscriptionSettings = field(default_factory=TranscriptionSettings)
    transform: TransformSettings = field(default_factory=TransformSettings)
    vocabulary: Vocabulary = field(default_factory=Vocabulary)
    output: OutputSettings = field(default_factory=OutputSettings)
    hotkey: HotkeySettings = field(default_factory=HotkeySettings)
    audio: AudioSettings = field(default_factory=AudioSettings)
    # How long discovered models stay fresh before a background re-fetch. The cache is
    # never discarded on expiry -- stale data with a visible timestamp beats an empty
    # picker when the network is down.
    model_cache_ttl_hours: int = 12
    # Actively probe whether the selected model honours optional parameters (reasoning
    # effort, priority tier) by sending a minimal request and reading the error. Costs a
    # negligible number of tokens, once per model, and is the only way to learn this for
    # providers that publish no capability metadata.
    probe_capabilities: bool = True
    # On by default: an app that silently goes stale is the problem yada exists to avoid.
    updates_enabled: bool = True


# --------------------------------------------------------------------------------------
# (De)serialisation
#
# Hand-rolled rather than pydantic: the schema is small, and this keeps the dependency
# list short enough that packaging stays boring.
# --------------------------------------------------------------------------------------

T = TypeVar("T")


def to_dict(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, list):
        return [to_dict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    return obj


def _unwrap_optional(tp: Any) -> Any:
    """Reduce `X | None` to `X`; leave anything else alone."""
    if typing.get_origin(tp) in (types.UnionType, typing.Union):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return tp


def from_dict(cls: type[T], data: Any) -> T:
    """Build `cls` from `data`, ignoring unknown keys and defaulting missing ones.

    Tolerant on purpose: a hand-edited config with a typo'd key should lose that key, not
    refuse to start the app.
    """
    if not is_dataclass(cls):
        return typing.cast(T, data)
    if not isinstance(data, dict):
        return cls()  # type: ignore[call-arg]

    hints = typing.get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue
        raw = data[f.name]
        tp = _unwrap_optional(hints.get(f.name, Any))
        origin = typing.get_origin(tp)
        if is_dataclass(tp) and isinstance(tp, type):
            kwargs[f.name] = from_dict(tp, raw)
        elif origin is list and isinstance(raw, list):
            (inner,) = typing.get_args(tp) or (Any,)
            if is_dataclass(inner) and isinstance(inner, type):
                kwargs[f.name] = [from_dict(inner, v) for v in raw]
            else:
                kwargs[f.name] = list(raw)
        else:
            kwargs[f.name] = raw
    return cls(**kwargs)  # type: ignore[call-arg]


def load(path: Path | None = None) -> Settings:
    p = path or config_path()
    if not p.exists():
        return Settings()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A corrupt config should not brick a tray app. Fall back to defaults; the UI
        # will show them and the next save overwrites the bad file.
        return Settings()
    return from_dict(Settings, data)


def save(settings: Settings, path: Path | None = None) -> None:
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename so an interrupted save cannot truncate a good config.
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(to_dict(settings), indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)
