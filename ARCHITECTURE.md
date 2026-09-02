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

**The realtime socket is opened with `?intent=transcription`, never `?model=`.** That
parameter names a realtime *conversation* model (`gpt-realtime` and friends), so passing a
transcription model to it is rejected with a 4000 `invalid_model` close frame. Two releases
tried it first with `?intent=transcription` as a fallback, and the fallback could never run:
the rejection arrives *after* the websocket handshake completes, so `connect()` returned
successfully and stopped there. What the user saw was "Live transcription unavailable" mid
recording, for a model that streams perfectly well.

The lesson generalises, so `connect()` now waits for `session.updated` before reporting
success. A handshake proves nothing about a session — the model and every field in
`session.update` are validated afterwards — and a refusal has to be a *connection* failure
for anything above it to fall back.

**The batch floor is not universal after all.** Measured against the live API:
`gpt-live-transcribe` answers `/v1/audio/transcriptions` with a bare HTTP 404, because it is
realtime-only; `gpt-transcribe`, `gpt-4o-transcribe` and `whisper-1` all work on both. So a
streaming failure on a realtime-only model leaves nothing to fall back to, and saying that
beats reporting a 404 nobody can act on. Empty transcripts now carry the accumulated
warnings for the same reason: "Transcription produced no text" on its own sent a user
looking for a fault in a microphone that was working fine.

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

### Nothing perceivable waits for the network

Starting a recording used to be strictly serial: open the realtime socket, then open the
microphone, then set the state and play the listening chime. Measured against the live API
the socket alone is 0.3-1.3s (DNS, TLS, handshake, and the `session.updated` round trip
added above), so pressing the shortcut appeared to do nothing for up to three seconds.

The latency was the smaller half of the problem. The microphone was opened *after* the
socket, so anything said in that window was not merely missing from the live transcript --
it was never recorded at all.

The order is now: attach the stream sink, open the microphone, set the state, chime, and
*then* open the socket as a background task. `StreamSink` already queues about twenty
seconds of audio and drops only past that, so the queued chunks are drained in order the
moment the pump starts and the live transcript still begins at the first word. Two
consequences worth knowing:

* A recording can be shorter than its own connect, so the stop path waits up to
  `STREAM_CONNECT_GRACE` for a socket still opening. Abandoning it would mean batch, and
  for a realtime-only model batch is an HTTP 404 and no transcript at all.
* The task must not key its "is this still wanted?" check on the state being `RECORDING` --
  by the time it runs on a short dictation the state has moved to `TRANSCRIBING`, and that
  is precisely the session the stop path is waiting for. It checks the session's identity
  instead.

PortAudio is warmed on a background thread at launch for the same reason: `import
sounddevice` initialises it and enumerating devices walks every endpoint the host offers,
which is not free on Windows and was otherwise paid on the first keypress.

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

Silent, per-user, no admin rights, no installer to sit through.

Windows cannot overwrite a running executable, so yada never tries. Every release lives in
its own directory:

```
%LOCALAPPDATA%\yada\  (Windows)      ~/.local/share/yada/  (Linux)
  current           active version, e.g. "0.1.5"
  versions/0.1.4/   previous, kept for rollback
  versions/0.1.5/   active — shortcuts point straight at this one's executable
  staging/          partial downloads; safe to delete
```

A check runs 60s after launch and every 6h. A newer release is downloaded, verified and
**fully extracted** while the app runs, so applying it is just starting yada again — the
running version notices a newer one, hands over to it, and exits. That is why the
"Restart to finish updating" action only appears once something is staged: restarting *is*
the install step.

### There is deliberately no launcher binary

There used to be. A small one-file PyInstaller executable sat at the install root as a
fixed path for shortcuts to target, read `current`, and exec'd the right version.

Windows Defender classified it as **Trojan:Win32/Bearfoos.A!ml** and removed it — together
with the Start Menu shortcut and the autostart registry key — roughly ninety seconds after
a real install. The one-*dir* application sitting beside it was never touched.

That verdict is reasonable in hindsight. A one-file build extracts itself to a temp
directory and executes code from there, which is what a dropper does, and this one then
wrote itself into `HKCU\...\Run` from a binary created seconds earlier. The `!ml` suffix
means it was a machine-learning heuristic rather than a signature, and self-extracting
unsigned executables are exactly what those models are tuned to catch.

So the design changed rather than the symptom being worked around:

* **No self-extracting executable is shipped.** Shortcuts point directly at
  `versions/<v>/yada[.exe]`, a one-dir build, which was never flagged.
* **The running version maintains its own shortcuts** (`app.py::_sync_desktop_integration`),
  repointing them after an update and recreating them if something removed them. That is
  the job the fixed path used to do.
* **Autostart is a setting the app applies**, not something the installer writes at drop
  time. It was one of the three resources Defender remediated.
* **`yada doctor` reports antivirus action against yada's files**, so the otherwise
  baffling "it installed and then vanished" has an answer on screen.

Code signing is the only real fix for reputation, and it is not free; Azure Trusted Signing
is the cheapest credible route if this becomes a recurring problem.

**Verification.** The updater executes code it downloaded, so: SHA-256 of each archive
checked against a `SHA256SUMS` release asset, and `SHA256SUMS` itself checked against an
Ed25519 signature using a public key compiled into the binary. HTTPS proves the bytes came
from GitHub; it says nothing about whether the maintainer or an account thief published
them. CI also verifies that the signature it just produced validates against the public key
*compiled into the binaries being shipped*, so pasting the wrong half of a keypair fails the
release instead of publishing something every client refuses.

**A version directory contains exactly one release, or the one it already had.**
Extraction builds into `.incoming-<v>-<pid>` and renames into place. Getting the *previous*
directory out of the way took three attempts, and the two failures are worth recording
because each looked correct.

First it was `shutil.rmtree(..., ignore_errors=True)`, then extraction into whatever
survived — so one locked file meant the new release landed alongside the old one and
`.complete` marked the mixture as trustworthy. That became a *verified* `rmtree`, which
refused instead of merging. Better, and still wrong in the worst way: rmtree deletes file
by file. A running copy on Windows holds `python3.dll` mapped and nothing else, so by the
time the delete failed, every other file was already gone. A user who double-clicked 0.1.10
while 0.1.10 was running got a shredded install that `current` still pointed at, and an app
that would not start — from a working install, one second earlier.

Now `core.swap_in` renames the old directory aside and only then moves the new one in. A
rename is all-or-nothing, so a locked file leaves the existing install **exactly** as it
was; if the second move fails, the old directory is renamed back. The displaced directory
is deleted best-effort as `.trash-<v>-<pid>-<rand>` and collected by a later prune if
something still holds it. Both `.incoming-` and `.trash-` are dot-prefixed, so
`installed_versions()` skips them and a leftover can never occupy a retention slot or
appear as a release.

**Installing over a running copy means ending it, and nothing else works.** Measured on
Windows 11, against a real running build: deleting a mapped DLL is refused, and *renaming
the directory that contains it is refused too* — so the swap above is a safety net, not a
way to replace a live install. The only thing that works is the process actually exiting.
`selfinstall.stop_running_instance` therefore asks over IPC, then watches the **processes**
until they are gone, and terminates any that ignore the request.

It used to wait for `ipc.is_running()` to go false, which is the wrong signal by a wide
margin: `CommandServer.stop()` runs early in shutdown, so the socket goes quiet seconds
before the process releases its files — and a one-second grace after that was what
authorised the delete described above. `procutil` finds them by executable path
(`EnumProcesses` + `QueryFullProcessImageNameW` on Windows, `/proc/<pid>/exe` on Linux)
rather than by asking politely, so it also works against copies of yada built before any
of this existed. On Linux a zombie reads as *gone*: signal 0 still succeeds against an
unreaped process, and treating it as alive would burn the whole timeout waiting for a
process that had already exited.

**Disk retention.** A version directory is roughly 190 MB, so this is not housekeeping
trivia. The two newest releases are kept and the rest deleted, and `staging/` is emptied
since a partial download is never resumed. Pruning runs at startup as well as after
staging an update, so an install that never receives one still reclaims space. The version
named by `current` is never deleted — there would be nothing to fall back to — which does
mean a pointer left behind by running a version directly keeps that version on disk.

**Failure is contained.** A `.complete` marker is written last and is the only thing trusted,
so an interrupted extraction is ignored rather than booted. A version that starts three
times without reporting healthy stops being chosen, and the previous release is still on
disk.

## Packaging, and three traps in it

Built with PyInstaller in **one-dir** mode. One-file re-extracts the whole bundle to a temp
directory on every launch, which adds seconds to startup — unacceptable for a tray app
expected to answer a keypress. One-dir also lets the updater swap a directory atomically,
which is exactly the shape the versioned layout wants.

Three things here were found by building and running, not by reading, and each would have
shipped a broken binary:

1. **PyInstaller runs its entry script as `__main__` with no package context.** A module using
   relative imports therefore cannot be the entry point — `from . import ipc` fails with
   "attempted relative import with no known parent package". Hence the tiny shim in
   `packaging/entry_app.py`, which imports by absolute path.
2. **One-file builds get quarantined.** See the auto-update section: the launcher was
   flagged as `Trojan:Win32/Bearfoos.A!ml` and no longer exists. Everything ships one-dir.
   `yada/updater/__init__.py` still imports lazily via PEP 562 `__getattr__` — `core` is
   pure standard library while `github` needs httpx — which keeps import cost off the
   startup path.
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
  relaunch.py       handing over to a newer installed version
  selfinstall.py    the app installs itself; there is no separate installer binary
  procutil.py       is that process still running, and end it if it will not go
```

## Config and secrets

Settings are JSON under `platformdirs.user_config_dir("yada")` — greppable, diffable,
hand-editable. API keys never go in that file; they go to `keyring`, which resolves to Windows
Credential Manager and KWallet on Plasma. Schema carries a `version` field from day one so
migrations are possible without guessing.
