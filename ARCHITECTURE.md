# yada — Architecture

*Yet Another Dictating App.*

A tray-resident push-to-talk dictation tool: hotkey → record → transcribe → (optional) LLM
transform → clipboard/paste. Windows 11 and KDE Plasma (Wayland) only.

Built to be extended by provider, not rewritten. Everything user-visible that could go stale
(models, providers, vocabulary, transform steps) is data at runtime, never a hardcoded enum.

## Non-goals

Deliberately absent to keep this maintainable by one person: macOS, mobile, browser extension,
multi-user sync, plugin sandboxing, telemetry, auto-update infrastructure, speaker diarization.

## The two hard constraints

Both come from Wayland, and both shape the module layout.

1. **A Wayland client cannot grab a global hotkey.** Hotkey delivery is therefore a swappable
   backend behind a single internal `toggle` command, so the app never cares where the trigger
   came from.
2. **A Wayland client cannot synthesize keystrokes.** Auto-paste is a *detected capability* that
   degrades to clipboard-only rather than a feature that silently fails.

## Layers

```
hotkey backend ─┐
tray menu ──────┼─→ IPC toggle ─→ Session state machine
CLI `yada` ──┘                      │
                                       ├─→ AudioCapture ──→ Tee ─┬─→ WavBuffer  (batch path)
                                       │                          └─→ StreamSink (streaming path,
                                       │                                          if provider supports)
                                       ├─→ TranscriptionProvider
                                       ├─→ TransformPipeline (ordered steps)
                                       └─→ Output (chime, clipboard, paste)
```

### Threading

Three threads, no `qasync`. Qt owns the main thread and all UI. One dedicated `asyncio` event
loop lives in a `QThread` and owns *every* network call (websocket, HTTP). `sounddevice` calls
back on its own PortAudio thread. Everything crosses boundaries as Qt signals; no shared mutable
state. This keeps the UI responsive during a slow transform and keeps the audio callback
non-blocking, which is the one place a stall is audible.

## Provider model

The load-bearing abstraction. Two independent axes — a provider may implement either or both.

### Transcription

```python
class TranscriptionCapabilities:
    streaming: bool       # realtime deltas while recording
    batch: bool           # whole-buffer upload
    keywords: bool        # native literal-term vocabulary hints
    prompt: bool          # free-form context prompt
    languages: bool       # expected-language hints
    delay_tuning: bool    # latency/accuracy dial
```

The pipeline reads `capabilities()` and picks a path. `streaming=False` means the stream sink is
never attached; the WAV buffer path is always present as the floor, so every provider works.

### Transformation

Every interesting transform provider speaks OpenAI-compatible `/chat/completions`. So there is
one `OpenAICompatChat` base parameterized by base URL, headers, and model-discovery strategy.
Adding Grok, Groq, Together, or a local llama.cpp server is a subclass with a URL — not a new
integration.

### Shipping now

| Provider | Transcribe | Transform | Notes |
|---|---|---|---|
| OpenAI | streaming + batch | yes | realtime WS; `keywords`, `prompt`, `languages`, `delay` |
| OpenRouter | batch | yes | no realtime; rich model metadata via `output_modalities` |

### Designed for, not yet built

ElevenLabs (batch STT), Grok/xAI (transform), Groq (fast batch Whisper), local
faster-whisper/whisper.cpp (batch, offline). Each is one file in `providers/` plus a registry
entry. No changes to the pipeline, UI, or config schema should be required.

## Model discovery — the whole point

Staleness is the failure mode yada exists to avoid, so discovery is a module
(`providers/catalog.py`), not a helper. Three rules:

1. **Nothing is hardcoded.** Model lists come from the provider at runtime. The curated
   rank tables only decide default ordering and what `""` (auto) resolves to; they never
   gate what is selectable, and free-text model entry always works.
2. **Capabilities are discovered too.** Where a provider publishes them we read them:
   OpenRouter returns `supported_parameters` per model, so "does this model take a
   reasoning budget" gets a real answer off the network. Where it does not — OpenAI's
   `/v1/models` returns ids and nothing else — we *probe*: send one minimal request
   exercising the parameter and classify the response. Costs a handful of tokens, once per
   model, and the verdict is cached ~forever because a given model id's capabilities do
   not change.
3. **Stale beats empty.** The cache is never discarded on expiry. Offline, the UI shows
   the last known models, when they were fetched, and why the refresh failed —
   *"Showing models discovered 4 days ago — last refresh failed: network down"*.

Anti-drift falls out of this. `resolve_model()` warns when a pinned model has vanished
from a freshly discovered list and names the highest-ranked replacement, and it
deliberately stays quiet when discovery merely failed — an empty list while offline proves
nothing about whether a model still exists.

## Capability support is tri-state, not boolean

`Support` is `SUPPORTED` / `UNKNOWN` / `UNSUPPORTED`, because the middle case is the
common one. OpenRouter fronts many upstream providers and often cannot say in advance
whether a model honours priority routing. Treating that as a boolean forces a bad choice:
hide an option that might work, or show one that silently does nothing.

`UNKNOWN` means the request is sent and may be ignored, and the UI says exactly that:

| Support | Label |
|---|---|
| `SUPPORTED` | Use priority processing (faster, costs more) |
| `UNKNOWN` | Try to use priority processing if this provider offers one |
| `UNSUPPORTED` | Priority processing (not available for this provider) |

Tooltips carry the billing implication, since a "try it" that succeeds costs real money.
Labels live in `providers/base.py` so widgets and providers cannot drift apart on wording.

Two concrete traps this shape avoids: OpenAI and OpenRouter accept *different* reasoning
effort values (OpenAI has `max` and no `minimal`; OpenRouter the reverse), and sending
OpenRouter both `reasoning` and `reasoning_effort` returns a 400. Valid values always come
from `capabilities()`, never from a shared enum rendered directly.

## Vocabulary — one source of truth

The user's uncommon terms and spellings live in config once, then fan out three ways:

1. Transcription `keywords` where the provider supports it natively (OpenAI does).
2. Injected into the transform step's system prompt.
3. Optionally as deterministic `find_replace` steps, which cost nothing and cannot be ignored
   by a model.

## Transformation pipeline

Borrowed from Whispering's design, which is good: a transformation is an **ordered list of
steps**; each step is `prompt_transform` (LLM call) or `find_replace` (literal or regex). Steps
compose, so "fix my known misspellings deterministically, then clean up grammar with an LLM" is
expressible without special-casing.

## Chimes

Two distinct sounds — one when transcription completes, one when transformation completes — via
`QSoundEffect` (low-latency, short WAVs). Independently toggleable, because the transform chime
is noise if no transform is configured.

## Auto-paste

Off by default. When on, the target is chosen explicitly: after transcription, or after
transformation. Clipboard write always happens; keystroke injection is best-effort behind a
detected backend:

- **Windows**: `SendInput` via ctypes. Reliable.
- **KDE/Wayland**: `ydotool` if a running daemon and `uinput` access are detected, else the
  `RemoteDesktop` portal, else unavailable.

When no backend is available the UI says so plainly at settings time rather than failing at
paste time.

## Auto-update

Silent, per-user, no admin rights, no installer the user has to watch.

Windows cannot overwrite a running executable, so yada never tries. Every release lives in
its own directory behind a stable launcher:

```
%LOCALAPPDATA%\yada\  (Windows)      ~/.local/share/yada/  (Linux)
  yada[.exe]        stable launcher — shortcuts point here, never changes
  current           active version, e.g. "0.3.1"
  versions/0.3.0/   previous, kept for rollback
  versions/0.3.1/   active
  versions/0.3.2/   downloaded, verified, extracted, waiting
  staging/          partial downloads; safe to delete
```

A check runs 60s after launch and every 6h. A newer release is downloaded, verified and
**fully extracted** while the app is running, so at next launch activation is one pointer
write — not an install. That is what makes the update either invisible or a one-second
blink, which is the standard being held to.

**Verification.** The updater executes code it downloaded, so: SHA-256 of each archive
checked against a `SHA256SUMS` release asset, and `SHA256SUMS` itself checked against an
Ed25519 signature using a public key compiled into the binary. HTTPS proves the bytes came
from GitHub; it says nothing about whether the maintainer or an account thief published
them. Unsigned releases are refused unless `allow_unsigned` is set, which exists for local
test builds only. Archives are also scanned for path traversal before anything is written.

**Failure is contained.** A `.complete` marker is written last and is the only thing the
launcher trusts, so an interrupted extraction is ignored rather than booted. A version that
starts three times without reporting healthy stops being chosen, and the previous release
is still on disk — a crash-on-launch release is an inconvenience, not a bricked app.

## Packaging, and three traps in it

Built with PyInstaller in **one-dir** mode. One-file re-extracts the whole bundle to a temp
directory on every launch, which adds seconds to startup — unacceptable for a tray app
expected to answer a keypress. One-dir also lets the updater swap a directory atomically,
which is exactly the shape the versioned layout wants.

Three things here were found by building and running, not by reading, and each would have
shipped a broken binary:

1. **PyInstaller runs its entry script as `__main__` with no package context.** A module using
   relative imports therefore cannot be the entry point — `from . import ipc` fails with
   "attempted relative import with no known parent package". Hence the tiny shims in
   `packaging/entry_app.py` and `packaging/entry_launcher.py`, which import by absolute path.
2. **The launcher must not import the updater package eagerly.** `yada/updater/__init__.py`
   used to import `github`, which imports httpx — deliberately excluded from the launcher
   bundle. The launcher crashed on startup. The package now imports `core` (pure standard
   library) eagerly and everything else lazily via PEP 562 `__getattr__`.
3. **PortAudio is not bundled by the Linux `sounddevice` wheel.** `sounddevice` is a single
   module that `dlopen()`s PortAudio at import, so PyInstaller cannot see the dependency by
   static analysis. The spec locates and bundles `libportaudio` explicitly, and *fails the
   build* if it cannot — a binary that starts and then silently cannot record is a far worse
   failure than one that does not build.

Also: builds are pinned to a uv-managed interpreter (`python-preference = "only-managed"`).
Distro system pythons frequently ship without `libpython3.x.so`, which PyInstaller requires
to build a bootloader, and the resulting error names a package rather than the real cause.

## Layout

```
src/yada/
  app.py            QApplication bootstrap and wiring
  config.py         settings dataclasses + JSON on disk (platformdirs)
  secrets.py        API keys via keyring (Credential Manager / KWallet)
  ipc.py            single-instance guard + local socket `toggle`
  audio/            capture (sounddevice), resample (soxr), tee, wav buffer
  providers/        base protocols, registry, openai, openrouter
  pipeline/         session state machine, transform step engine
  hotkey/           base protocol, win32, kde_portal, external
  output/           clipboard, paste backends, chime
  ui/               tray, settings window
  updater/          install layout, GitHub releases, verification, background service
  launcher.py       stable shim that release directories sit behind
```

## Config and secrets

Settings are JSON under `platformdirs.user_config_dir("yada")` — greppable, diffable,
hand-editable. API keys never go in that file; they go to `keyring`, which resolves to Windows
Credential Manager and KWallet on Plasma. Schema carries a `version` field from day one so
migrations are possible without guessing.
