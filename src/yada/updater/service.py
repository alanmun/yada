"""The background update loop.

Behaviour the user actually sees: nothing. A check runs shortly after launch and then on
an interval. If a newer release exists it is downloaded, verified and extracted while the
app keeps running, so by the time it matters the work is already done and activation is a
single pointer write at next launch.

Deliberately quiet about failures. A failed update check is not an event worth interrupting
someone mid-dictation for; it is recorded in `status` for the settings pane and retried on
the next tick.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from .core import prune_old_versions
from .github import (
    Release,
    UpdateError,
    download_and_verify,
    extract_release,
    fetch_latest,
    update_available,
)

# Checked a minute after startup rather than immediately, so an update check never competes
# with showing the tray icon.
INITIAL_DELAY_SECONDS = 60.0


@dataclass(slots=True)
class UpdateStatus:
    """What the settings pane shows. Every field is safe to display verbatim."""

    enabled: bool = True
    checking: bool = False
    last_checked: str | None = None
    last_error: str | None = None
    # Downloaded, verified, extracted and waiting for the next launch.
    ready_version: str | None = None
    available_version: str | None = None
    downloading: bool = False
    progress: float = 0.0  # 0..1, -1 when the server sends no content-length

    def summary(self) -> str:
        if not self.enabled:
            return "Automatic updates are off."
        if self.ready_version:
            return f"Version {self.ready_version} is ready — it will be used next launch."
        if self.downloading:
            pct = f"{self.progress * 100:.0f}%" if self.progress >= 0 else "in progress"
            return f"Downloading {self.available_version or 'update'} ({pct})…"
        if self.checking:
            return "Checking for updates…"
        if self.last_error:
            return f"Last check failed: {self.last_error}"
        if self.last_checked:
            return f"Up to date (checked {self.last_checked})."
        return "No update check yet."


class UpdateService:
    """Owns the periodic check. Runs on the app's asyncio thread."""

    def __init__(
        self,
        *,
        repo: str,
        current_version: str,
        interval_hours: float = 6.0,
        allow_unsigned: bool = False,
        include_prerelease: bool = False,
        on_change: Callable[[UpdateStatus], None] | None = None,
    ) -> None:
        self.repo = repo
        self.current_version = current_version
        self.interval_hours = interval_hours
        self.allow_unsigned = allow_unsigned
        self.include_prerelease = include_prerelease
        self.status = UpdateStatus()
        self._on_change = on_change
        self._task: asyncio.Task | None = None

    # -- lifecycle ----------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def _notify(self) -> None:
        if self._on_change:
            self._on_change(self.status)

    async def _loop(self) -> None:
        try:
            await asyncio.sleep(INITIAL_DELAY_SECONDS)
            while True:
                if self.status.enabled and not self.status.ready_version:
                    await self.check_now()
                await asyncio.sleep(self.interval_hours * 3600)
        except asyncio.CancelledError:
            raise

    # -- one pass -----------------------------------------------------------------------

    async def check_now(self) -> UpdateStatus:
        """Check, and if there is something newer, fetch and stage it.

        Never raises: an update failure must not be able to disturb a running app.
        """
        self.status.checking = True
        self.status.last_error = None
        self._notify()
        try:
            release = await fetch_latest(self.repo, include_prerelease=self.include_prerelease)
            if release is None or not update_available(release, self.current_version):
                self.status.available_version = None
                return self.status
            self.status.available_version = release.version
            self._notify()
            await self._stage(release)
        except UpdateError as exc:
            self.status.last_error = str(exc)
        except Exception as exc:  # noqa: BLE001 - defensive: never take the app down
            self.status.last_error = f"unexpected: {exc}"[:200]
        finally:
            self.status.checking = False
            self.status.downloading = False
            self.status.last_checked = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
            self._notify()
        return self.status

    async def _stage(self, release: Release) -> None:
        def progress(done: int, total: int) -> None:
            self.status.progress = (done / total) if total else -1.0
            self._notify()

        self.status.downloading = True
        self.status.progress = 0.0
        self._notify()

        archive = await download_and_verify(
            release, allow_unsigned=self.allow_unsigned, on_progress=progress
        )
        # Extraction is eager and off-thread: doing it now is what makes next launch
        # instant instead of showing an installer.
        await asyncio.to_thread(extract_release, archive, release.version)
        self.status.ready_version = release.version
        self.status.downloading = False
        await asyncio.to_thread(prune_old_versions)
        self._notify()
