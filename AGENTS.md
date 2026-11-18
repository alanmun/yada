# Notes for coding agents

## Cutting a release

1. **Check the recommended models are still current** (see below). Once per coding
   session: if you already ran it for an earlier release in this session, skip it.
2. Wait for CI on the commit you are releasing to pass on Linux and Windows.
3. Bump the version in both `pyproject.toml` and `src/yada/__init__.py`, and commit as
   `0.1.N: <summary>`.
4. Tag with an annotated tag, `git tag -a v0.1.N -m "yada 0.1.N: <summary>"`, and push
   `main` and the tag. The Release workflow builds, signs and publishes the archives.

## Recommended-model check

yada steers users to a curated model per provider: `recommended_transcription` and
`recommended_transform` in `src/yada/providers/registry.py`, plus the matching defaults in
`src/yada/config.py`. These go stale quietly, because nothing fails when a better model
ships. Before a release:

1. For each provider, take the first entry of each recommended tuple **that the provider
   actually offers**. That is the pick users get. OpenRouter's tuples mirror OpenAI's and
   lead with models it does not carry (`openai/gpt-live-transcribe`), by design.
2. Find when it was released. Providers report a `created` Unix timestamp per model:
   - OpenAI: `GET /v1/models` (needs a key), or the cache yada keeps of it at
     `~/.cache/yada/catalog.json` (Linux).
   - OpenRouter: `GET https://openrouter.ai/api/v1/models`, public, no key needed. It
     leaves out transcription models unless asked:
     `GET https://openrouter.ai/api/v1/models?output_modalities=transcription`.
3. If the pick is **more than a year old**:
   - **An obvious successor exists** (same tier in a newer family, e.g.
     `gpt-5.6-luna` → `gpt-6-luna`): propose the bump, with the successor's release
     date, and make it only once the user agrees.
   - **No obvious successor exists**: tell the user before releasing. Name the model, its
     age, and what the provider now offers, so they can decide.
4. If a pick that used to be offered **no longer appears**, tell the user the same way.
   `ProviderSpec.recommended` quietly falls through to the next candidate, so nothing
   breaks visibly.

Report the result in one line even when nothing is stale, so the user knows it ran.
