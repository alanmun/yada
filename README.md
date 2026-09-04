# yada

*Yet Another Dictating App.*

Press a shortcut, speak, get text. Optionally use transformations to have an LLM clean it up first. 

For Windows and Linux: Lives in the system tray on Windows 11 and KDE Plasma.

```
Ctrl+Shift+;  ──▶  🔴 recording  ──▶  Ctrl+Shift+;  ──▶  ♪ transcript  ──▶  ♪ cleaned up
                    (streaming as you speak)              chime 1              chime 2
```

## Features

- **One shortcut that works anywhere**: press once to start, once to stop, and your spoken word is turned into text. 
  Sounds play when starting your transcription and when its ready to paste.
- **Live transcription**: with OpenAI, audio streams while you speak, so the text is ready the instant you stop. Providers without a live connection transcribe after you stop.
- **Transformations**: After each transcribe completes, you can run optional LLM cleanup passes or find and replace operations. A third chime
- **A vocabulary that actually works**: your names and jargon are sent as *literal vocabulary
  hints* to the transcription model, fixing spelling while it is still listening rather than
  patching it afterwards.
- **Opt-in pasting**: off by default. Choose to paste after transcription or after cleanup.
- **Silent auto-update**: new versions download and unpack in the background; the next
  launch is already the new one. Signature-verified, with automatic rollback.
- **Nothing is lost**: every recording is buffered locally, so a dropped connection costs a
  second, not your words. A failed cleanup pass still gives you the transcript.

## Why this exists

I really enjoyed using [Whispering](https://github.com/epicenter-md/epicenter), but the app like many apps went stale and stopped receiving updates. In the age of AI I figured I'd just make my own, exactly the way I like it. I'll speak to my design preferences/quality goals here:

- **No more getting stuck on last year's models**: Models are discovered at runtime whenever possible. You can even set the model to *Automatic* and it follows whatever the provider currently recommends. New models are usable the day they ship, without an update to yada. 
  - **Capabilities are discovered too.** Where a provider publishes what a model supports, yada reads it. Where it does not, yada measures it with a single cheap request and remembers the answer.
- **Providers are pluggable.** OpenAI and OpenRouter are supported today, future integrations should be easily extensible without rewriting 6 python files.
- **Privacy**: Local models are supported, and you can upload custom ones and configure them yourself, so you're not forced to use only what an app officially supports.
- **Low RAM util**: A speech to text app shouldn't be a memory hog.
  - (This doesn't count RAM used to load models locally!)


## Install

Download the archive for your platform from [Releases](https://github.com/alanmun/yada/releases), extract the whole folder, and **double-click `yada`** (`yada.exe` on Windows). It installs itself under your own user profile and starts. No command line, no administrator rights, and nothing to run as admin.

After that yada keeps itself up to date: it checks in the background, downloads and verifies the next release while you work, and applies it the next time it starts.

### Troubleshooting for Windows

Windows might warn that the publisher is unrecognised, because these binaries are not code-signed: choose _More info_ then _Run anyway_.

Rarely, Windows Defender removes an unsigned program that sets itself to start with Windows. If yada installs and then vanishes — no tray icon, and a Start Menu shortcut that does nothing — that is what happened. To get it back:

1. **Confirm it.** Run `yada doctor` (see below); it reports what Defender has taken, if anything. Or open **Windows Security → Virus & threat protection → Protection history** and look for an item naming `yada`.
2. **Restore the file.** In Protection history, open that item and choose **Actions → Restore**. Defender will keep quarantining it unless you do the next step too.
3. **Allow the folder.** **Virus & threat protection → Manage settings → Exclusions → Add or remove exclusions → Add an exclusion → Folder**, and choose:

   ```
   %LOCALAPPDATA%\yada
   ```

   That is yada's own folder inside your user profile, so this does not lower protection anywhere else.
4. **Start yada again** from the Start Menu, or re-run `yada.exe` from the extracted folder.

If you would rather not add an exclusion, yada still works if you start it yourself each time — it is only the "start with Windows" registration that triggers this. Turn that off in **Settings → System → Start yada when I log in**.

It is also worth [reporting the file to Microsoft as a false positive](https://www.microsoft.com/en-us/wdsi/filesubmission), which is what actually gets the detection removed for everyone. The real fix is code signing, which is on the list.

Then open Settings from the tray icon and paste an API key.

If anything looks wrong, yada can diagnose itself:

```
Windows:  %LOCALAPPDATA%\yada\yada.exe doctor
Linux:    ~/.local/share/yada/yada doctor
```

It reports on the microphone, tray, keyring, shortcut backend, paste capability and API
keys, and names the fix for anything missing.

## The Wayland situation

Wayland client is not permitted to grab keyboard shortcuts or to press keys on your behalf because both of those things are security properties of the protocol.

yada asks the compositor to own the shortcut via the XDG `GlobalShortcuts` portal.
KDE Plasma supports this well where you just need to approve a dialog once. 


> If that is unavailable, bind this command in *System Settings → Shortcuts* instead, and it will reach yada just as fast:
> ```
> ~/.local/share/yada/yada toggle
> ```

**Auto-paste.** Text is always copied to your clipboard, which needs no special permission.
Pressing Ctrl+V for you needs nothing installed on any of the three:

| Session | How | Setup |
|---|---|---|
| Windows | `SendInput` | none |
| X11 | the `XTEST` extension | none |
| Wayland | the XDG `RemoteDesktop` portal | approve one dialog |

On Wayland the portal is the sanctioned way to ask: the compositor prompts you once, and
yada then synthesises the keystroke *through* it rather than around it. yada remembers the
portal's token, so the prompt does not come back on every launch.

If your desktop has no RemoteDesktop portal, [`ydotool`](https://github.com/ReimuNotMoe/ydotool)
still works, by injecting below the compositor:

```sh
sudo apt install ydotool
sudo systemctl enable --now ydotoold
sudo usermod -aG input $USER   # then log out and back in
```

With none of those, auto-paste is unavailable and yada says so in Settings rather than
failing quietly at paste time.

## Configuration

| What | Where |
|---|---|
| Settings | `~/.config/yada/settings.json` · `%APPDATA%\yada\settings.json` |
| API keys | OS keyring (KWallet / Credential Manager), or a `0600` file if none exists |
| Discovered models | `~/.cache/yada/catalog.json` · `%LOCALAPPDATA%\yada\Cache` |
| Installed versions | `~/.local/share/yada/` · `%LOCALAPPDATA%\yada\` — two kept, ~190 MB each |

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

## Licence

MIT
