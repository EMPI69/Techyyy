"""Local transcript files: saving, lookup for the dashboard, and the 14-day rolling purge.

Files live in Config.transcripts_dir (./transcripts/ by default) as
``<channel_id>__<channel-name>.html``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from discord.ext import commands, tasks

from ..common import stop_loop

if TYPE_CHECKING:
    from ..main import FAQBot

log = logging.getLogger("faq_bot")


def _transcript_files(directory: Path, channel_id: int) -> list[Path]:
    return sorted(directory.glob(f"{channel_id}__*.html"))


def save_html_transcript(directory: Path, channel_id: int, channel_name: str, text: str) -> Path:
    """Write ``<channel_id>__<name>.html``, replacing older copies (the name changes on close/re-open).

    Blocking file I/O: call via asyncio.to_thread.
    """
    directory.mkdir(exist_ok=True)
    safe_name = re.sub(r"[^a-z0-9_-]", "", channel_name.lower())[:90] or "ticket"
    path = directory / f"{channel_id}__{safe_name}.html"
    tmp = path.with_suffix(".html.partial")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)  # atomic: the dashboard never serves a half-written file
    for old in _transcript_files(directory, channel_id):
        if old != path:
            old.unlink(missing_ok=True)
    return path


def find_transcript(directory: Path, channel_id: int) -> Path | None:
    files = _transcript_files(directory, channel_id)
    return files[-1] if files else None


TRANSCRIPT_RETENTION_DAYS = 14
# Only these are transcripts; anything else in the folder (.gitkeep, notes, subfolders) is left alone.
TRANSCRIPT_SUFFIXES = frozenset({".html", ".txt"})
# A ".partial" is a write in progress (save_html_transcript renames it when done). Only
# ones this old are treated as leftovers from a crash and removed.
PARTIAL_GRACE_SECONDS = 60 * 60


def purge_old_transcripts(
    directory: Path, now: float | None = None, retention_days: int = TRANSCRIPT_RETENTION_DAYS
) -> list[Path]:
    """Delete .html/.txt transcripts last written more than ``retention_days`` ago.

    Age is the file's modification time: a transcript is rewritten when its ticket is
    closed and again when it's deleted, so the window runs from its last update.
    Blocking file I/O: call via asyncio.to_thread.
    """
    if not directory.is_dir():
        return []
    now = time.time() if now is None else now
    cutoff = now - retention_days * 86400
    removed: list[Path] = []
    for path in directory.iterdir():
        try:
            if path.is_symlink() or not path.is_file():
                continue  # never follow links or touch directories
            if path.name.endswith(".partial"):
                if path.stat().st_mtime < now - PARTIAL_GRACE_SECONDS:
                    path.unlink(missing_ok=True)
                    removed.append(path)
                continue
            if path.suffix.lower() not in TRANSCRIPT_SUFFIXES:
                continue
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed.append(path)
        except OSError:
            # One unreadable/locked file must not stop the rest of the sweep.
            log.warning("Could not check or remove transcript file %s", path, exc_info=True)
    return removed


class TranscriptCleanup(commands.Cog, name="TranscriptCleanup"):
    """Daily sweep of the local transcript folder (first run shortly after start-up)."""

    def __init__(self, bot: FAQBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self._purge_loop.start()

    async def cog_unload(self) -> None:
        await stop_loop(self._purge_loop)

    @tasks.loop(hours=24)
    async def _purge_loop(self) -> None:
        try:
            removed = await asyncio.to_thread(purge_old_transcripts, self.bot.config.transcripts_dir)
        except Exception:
            # Never let a failed sweep end the loop; tomorrow's run tries again.
            log.exception("Transcript cleanup failed")
            return
        for path in removed:
            log.info("Removed expired transcript (older than %d days): %s", TRANSCRIPT_RETENTION_DAYS, path.name)

    @_purge_loop.before_loop
    async def _before_purge(self) -> None:
        await self.bot.wait_until_ready()
