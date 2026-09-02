# yada

*Yet Another Dictating App.*

Press a shortcut, speak, get text. Optionally have an LLM clean it up first. Lives in the
system tray on Windows 11 and KDE Plasma.

```
Ctrl+Shift+;  ──▶  🔴 recording  ──▶  Ctrl+Shift+;  ──▶  ♪ transcript  ──▶  ♪ cleaned up
                    (streaming as you speak)              chime 1              chime 2
```

## Why this exists

Dictation tools rot. The models they name in their source go out of date, the provider
renames its recommended model, and a year later you are paying for last year's accuracy
through an app nobody has updated. yada is built around avoiding exactly that.

- **Models are discovered at runtime, never hardcoded.** Set the model to *Automatic* and it
  follows whatever the provider currently recommends. New models are usable the day they
  ship, without an update to yada.
- **Capabilities are discovered too.** Where a provider publishes what a model supports, yada
  reads it. Where it does not, yada measures it with a single cheap request and remembers the
  answer.
- **Providers are pluggable.** OpenAI and OpenRouter today. Adding ElevenLabs, Groq, Grok or a
  local Whisper is one file and a registry entry, with no changes to the recording pipeline.

## Features

- **Global shortcut** — press once to start, once to stop. Works from anywhere.
- **Live transcription** — with OpenAI, audio streams while you speak, so the text is ready
  the instant you stop. Providers without a live connection transcribe after you stop.
- **Two-stage pipeline** — transcription, then an optional LLM cleanup pass. A distinct chime
  for each, so you can tell them apart without looking.
- **A vocabulary that actually works** — your names and jargon are sent as *literal vocabulary
  hints* to the transcription model, fixing spelling while it is still listening rather than
  patching it afterwards.
- **Opt-in pasting** — off by default. Choose to paste after transcription or after cleanup.
- **Silent auto-update** — new versions download and unpack in the background; the next
  launch is already the new one. Signature-verified, with automatic rollback.
- **Nothing is lost** — every recording is buffered locally, so a dropped connection costs a
  second, not your words. A failed cleanup pass still gives you the transcript.

## Install

Download the archive for your platform from
[Releases](https://github.com/alanmun/yada/releases), extract it, and **double-click
`INSTALL`** (`INSTALL.exe` on Windows). That is the whole process — no command line, no
administrator rights. Everything goes under your own user profile.

Windows will warn that the publisher is unrecognised, because these binaries are not
code-signed: choose **More info** then **Run anyway**.

If yada installs and then disappears — no tray icon, a Start Menu shortcut that will not
start — check Windows Security → Protection history. Unsigned programs that start with
Windows do get caught by heuristics, and `yada doctor` will tell you if that has happened.

Then open Settings from the tray icon and paste an API key.

If anything looks wrong, yada can diagnose itself:

```
Windows:  %LOCALAPPDATA%\yada\yada.exe doctor
Linux:    ~/.local/share/yada/yada doctor
```

It reports on the microphone, tray, keyring, shortcut backend, paste capability and API
keys, and names the fix for anything missing.

## The Wayland situation

Worth knowing up front, because it affects two features and is not yada's doing: a Wayland
client is not permitted to grab keyboard shortcuts or to press keys on your behalf. Both are
security properties of the protocol.

**The shortcut.** yada asks the compositor to own it, via the XDG `GlobalShortcuts` portal.
KDE Plasma supports this well — you approve a dialog once. If that is unavailable, bind this
command in *System Settings → Shortcuts* instead, and it will reach yada just as fast:

```
~/.local/share/yada/yada toggle
```

**Auto-paste.** Text is always copied to your clipboard, which needs no special permission.
Pressing Ctrl+V for you requires [`ydotool`](https://github.com/ReimuNotMoe/ydotool):

```sh
sudo apt install ydotool
sudo systemctl enable --now ydotoold
sudo usermod -aG input $USER   # then log out and back in
```

Without it, auto-paste is unavailable and yada says so in Settings rather than failing
quietly at paste time. On Windows both features work with no setup.

## Configuration

| What | Where |
|---|---|
| Settings | `~/.config/yada/settings.json` · `%APPDATA%\yada\settings.json` |
| API keys | OS keyring (KWallet / Credential Manager), or a `0600` file if none exists |
| Discovered models | `~/.cache/yada/catalog.json` · `%LOCALAPPDATA%\yada\Cache` |
| Installed versions | `~/.local/share/yada/` · `%LOCALAPPDATA%\yada\` |

Settings are plain JSON, safe to hand-edit while yada is closed. API keys are never written
there. A key entered once works from both a source checkout and the installed app, so there
is nothing to re-enter when you switch between them.

`OPENAI_API_KEY` and `OPENROUTER_API_KEY` in the environment override whatever is stored.

## Development

```sh
uv sync --all-extras
uv run python -m yada                       # run it
uv run pytest -q                            # tests (no microphone or network needed)
uv run ruff check src tests scripts
uv run python scripts/spike_realtime.py     # probe the live API, needs OPENAI_API_KEY
uv run python scripts/make_chimes.py        # regenerate the notification sounds
```

`ARCHITECTURE.md` covers the design and, more usefully, why each awkward part is the way it
is.

### Releasing

```sh
uv run python scripts/gen_signing_key.py    # once: private key → GitHub secret,
                                            #       public key → updater/github.py
git tag v0.2.0 && git push --tags           # CI builds, signs and publishes
```

Installed copies pick the release up in the background within a few hours and use it at next
launch.

## Non-goals

Kept out deliberately, so this stays maintainable by one person: macOS, mobile, a browser
extension, sync, telemetry, speaker diarisation, and real-time translation.

## Prior art

yada exists because [Whispering](https://github.com/epicenter-md/epicenter) got the shape
right and then stopped being updated. The two-stage transcribe-then-transform pipeline and
the ordered-steps model for transformations are its ideas, and they are good ones.

## Licence

MIT
