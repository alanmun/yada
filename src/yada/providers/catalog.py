"""Live model and capability discovery, with a cache that survives being offline.

Model staleness is the failure mode yada exists to avoid, so discovery is a first-class
module rather than a helper. Three rules:

1. **Nothing is hardcoded.** Model lists come from the provider at runtime. Curated
   rankings only decide default ordering; they never gate what is selectable.
2. **Capabilities are discovered, not assumed.** Where a provider publishes them
   (OpenRouter's `supported_parameters`) we read them. Where it does not (OpenAI's
   /v1/models exposes none) we probe: send one minimal request with the parameter and read
   the error. Costs a negligible number of tokens, once per model, and is the only way to
   actually know.
3. **Stale beats empty.** The cache is never discarded on expiry. Offline, the app shows
   the last known models plus when they were fetched and why the refresh failed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from ..config import cache_dir
from .base import Modality, ModelInfo, Support

CATALOG_VERSION = 1


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def humanize_age(delta: timedelta | None) -> str:
    if delta is None:
        return "never"
    secs = int(delta.total_seconds())
    if secs < 90:
        return "just now"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= size:
            n = secs // size
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


# --------------------------------------------------------------------------------------
# Capability probing
# --------------------------------------------------------------------------------------


class CapabilityProber(Protocol):
    """Implemented by providers that publish no capability metadata.

    Should send the cheapest possible request exercising `parameter` and classify the
    outcome. Must never raise for a rejected parameter -- that is the answer, not an error.
    """

    async def probe_parameter(self, model: str, parameter: str) -> tuple[Support, str]: ...


@dataclass(slots=True)
class ProbeResult:
    parameter: str
    support: str  # Support value
    checked_at: str
    detail: str = ""

    @property
    def as_support(self) -> Support:
        try:
            return Support(self.support)
        except ValueError:
            return Support.UNKNOWN


# --------------------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class CatalogEntry:
    provider: str
    models: list[ModelInfo] = field(default_factory=list)
    fetched_at: str | None = None
    # Why the last refresh failed, if it did. Retained alongside good data so the UI can
    # say "showing models from 4 days ago -- couldn't reach the provider" rather than
    # silently presenting stale options as current.
    last_error: str | None = None
    # model id -> parameter -> result
    probes: dict[str, dict[str, ProbeResult]] = field(default_factory=dict)

    @property
    def age(self) -> timedelta | None:
        ts = _parse(self.fetched_at)
        return None if ts is None else datetime.now(UTC) - ts

    def staleness_note(self) -> str:
        """One line for the settings pane, honest about what the user is looking at."""
        if self.fetched_at is None:
            return "Models not discovered yet."
        age = humanize_age(self.age)
        if self.last_error:
            return f"Showing models discovered {age} — last refresh failed: {self.last_error}"
        return f"Models discovered {age}."

    def for_modality(self, modality: Modality) -> list[ModelInfo]:
        return [m for m in self.models if m.modality == modality]

    def support_for(self, model: str, parameter: str, fallback: Support) -> Support:
        """Probe result wins over the provider's own baseline, since it is measured."""
        probe = self.probes.get(model, {}).get(parameter)
        return probe.as_support if probe else fallback


def _model_to_dict(m: ModelInfo) -> dict:
    d = dataclasses.asdict(m)
    d["modality"] = str(m.modality)
    d["supported_parameters"] = list(m.supported_parameters)
    return d


def _model_from_dict(d: dict) -> ModelInfo:
    known = {f.name for f in dataclasses.fields(ModelInfo)}
    kwargs = {k: v for k, v in d.items() if k in known}
    kwargs["modality"] = Modality(kwargs.get("modality", "text"))
    kwargs["supported_parameters"] = tuple(kwargs.get("supported_parameters") or ())
    return ModelInfo(**kwargs)


class ModelCatalog:
    """Disk-backed discovery cache, keyed by provider id."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (cache_dir() / "catalog.json")
        self._entries: dict[str, CatalogEntry] = {}
        self._lock = asyncio.Lock()
        self.load()

    # -- persistence --------------------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if raw.get("version") != CATALOG_VERSION:
            return  # forward/backward incompatible: rediscover rather than guess
        for pid, body in (raw.get("providers") or {}).items():
            self._entries[pid] = CatalogEntry(
                provider=pid,
                models=[_model_from_dict(m) for m in body.get("models") or []],
                fetched_at=body.get("fetched_at"),
                last_error=body.get("last_error"),
                probes={
                    mid: {
                        param: ProbeResult(**pr)
                        for param, pr in params.items()
                        if isinstance(pr, dict)
                    }
                    for mid, params in (body.get("probes") or {}).items()
                },
            )

    def save(self) -> None:
        payload = {
            "version": CATALOG_VERSION,
            "providers": {
                pid: {
                    "models": [_model_to_dict(m) for m in e.models],
                    "fetched_at": e.fetched_at,
                    "last_error": e.last_error,
                    "probes": {
                        mid: {p: dataclasses.asdict(r) for p, r in params.items()}
                        for mid, params in e.probes.items()
                    },
                }
                for pid, e in self._entries.items()
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    # -- reads --------------------------------------------------------------------------

    def entry(self, provider_id: str) -> CatalogEntry:
        return self._entries.setdefault(provider_id, CatalogEntry(provider=provider_id))

    def is_stale(self, provider_id: str, ttl_hours: int) -> bool:
        age = self.entry(provider_id).age
        return age is None or age > timedelta(hours=ttl_hours)

    def resolve_model(
        self,
        provider_id: str,
        pinned: str,
        modality: Modality,
        *,
        auto_select_newest: bool = True,
    ) -> tuple[str, str | None]:
        """Decide which model to actually use.

        Returns (model_id, warning). The warning is the anti-drift mechanism: if a pinned
        model has vanished from a freshly discovered list, say so and name the replacement
        rather than failing on the next request.
        """
        entry = self.entry(provider_id)
        available = entry.for_modality(modality)
        best = available[0].id if available else ""

        if not pinned:
            if not auto_select_newest:
                return "", "No model selected and auto-select is off."
            if not best:
                return "", "No models discovered yet for this provider."
            return best, None

        # Only warn when discovery actually succeeded; an empty list while offline proves
        # nothing about whether the pinned model still exists.
        if (
            available
            and entry.fetched_at
            and not entry.last_error
            and not any(m.id == pinned for m in available)
        ):
            return pinned, (
                f"{pinned!r} is no longer offered by this provider. "
                f"Highest-ranked available is {best!r}."
            )
        return pinned, None

    # -- refresh ------------------------------------------------------------------------

    async def refresh(self, provider_id: str, provider, *, modality: Modality) -> CatalogEntry:
        """Re-discover models for one provider/modality.

        On failure the previous models are kept and `last_error` is set. Never raises --
        a background refresh must not be able to take the app down.
        """
        entry = self.entry(provider_id)
        try:
            models = await provider.list_models()
        except Exception as exc:  # noqa: BLE001 - any provider failure is just staleness
            entry.last_error = str(exc)[:200]
            async with self._lock:
                self.save()
            return entry

        # Replace only this modality's slice, so refreshing transcription models does not
        # wipe discovered transform models for the same provider.
        entry.models = [m for m in entry.models if m.modality != modality] + list(models)
        entry.fetched_at = _now()
        entry.last_error = None
        async with self._lock:
            self.save()
        return entry

    async def probe(
        self,
        provider_id: str,
        prober: CapabilityProber,
        model: str,
        parameters: list[str],
        *,
        recheck_after_days: int = 30,
    ) -> dict[str, Support]:
        """Measure parameter support for one model, caching the verdict.

        A given model id's capabilities do not change, so results are cached for a long
        time. Re-checked eventually only to recover from a probe that failed for an
        unrelated reason (rate limit, transient 500).
        """
        entry = self.entry(provider_id)
        per_model = entry.probes.setdefault(model, {})
        out: dict[str, Support] = {}
        dirty = False

        for param in parameters:
            cached = per_model.get(param)
            if cached is not None:
                checked = _parse(cached.checked_at)
                fresh_enough = checked is not None and (
                    datetime.now(UTC) - checked < timedelta(days=recheck_after_days)
                )
                if fresh_enough and cached.as_support is not Support.UNKNOWN:
                    out[param] = cached.as_support
                    continue
            try:
                support, detail = await prober.probe_parameter(model, param)
            except Exception as exc:  # noqa: BLE001
                support, detail = Support.UNKNOWN, str(exc)[:160]
            per_model[param] = ProbeResult(
                parameter=param, support=str(support), checked_at=_now(), detail=detail
            )
            out[param] = support
            dirty = True

        if dirty:
            async with self._lock:
                self.save()
        return out
