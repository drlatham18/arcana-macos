"""Read Arena's own local card database so grpIds become real card names.

Arena ships `Raw_CardDatabase_<hash>.mtga`, a plain SQLite file, next to the
client. It maps grpId -> title/type localization ids, ability ids -> rules text,
and prompt ids -> the exact sentence the client shows. Reading it keeps the
companion completely offline and always matches the installed client version.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from functools import lru_cache
from pathlib import Path

# macOS Arena installations and downloaded data.
_INSTALL_ROOTS = (
    "/Applications/MTGA.app",
    "/Applications/Magic The Gathering Arena.app",
    "/Users/Shared/Epic Games/MagicTheGathering/MTGA.app",
)
_MARKUP = re.compile(r"</?[A-Za-z][^>]*>")


# Locating the database globs several install directories, which is far too
# expensive to repeat for every engine state the log reader replays. Arena only
# swaps the file on a client update, so a short recheck window costs nothing.
_LOCATE_INTERVAL_SECONDS = 30.0
_located: tuple[float, Path | None] | None = None


def find_card_database() -> Path | None:
    """Return the newest readable Raw_CardDatabase file, or None."""
    global _located
    override = os.environ.get("MTGA_CARD_DB")
    if override:
        return Path(override) if Path(override).is_file() else None
    if _located is not None and time.monotonic() - _located[0] < _LOCATE_INTERVAL_SECONDS:
        return _located[1]

    roots: list[Path] = []
    roots.extend(Path(root) for root in _INSTALL_ROOTS)

    roots.extend([
        Path.home() / "Applications/MTGA.app",
        Path.home() / "Library/Application Support/com.wizards.mtga",
        Path.home() / "Library/Application Support/Steam/steamapps/common/MTGA",
        Path("/Users/Shared/Epic Games/MagicTheGathering"),
    ])
    override_root = os.environ.get("MTGA_INSTALL_ROOT")
    if override_root:
        roots.insert(0, Path(override_root))
    candidates: list[Path] = []
    for root in roots:
        if root.is_dir():
            try:
                candidates.extend(root.rglob("Raw_CardDatabase_*.mtga"))
            except OSError:
                pass
    newest = max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None
    _located = (time.monotonic(), newest)
    return newest


class CardDatabase:
    """Read-only lookups against the installed Arena card database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        # Read-only URI keeps the running client's file untouched and works
        # while Arena holds it open.
        self._connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
        self._connection.execute("PRAGMA query_only = ON")

    def close(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error:
            pass

    @lru_cache(maxsize=8192)
    def localized(self, loc_id: int | None) -> str:
        if not loc_id:
            return ""
        rows = self._connection.execute(
            "SELECT Formatted, Loc FROM Localizations_enUS WHERE LocId = ?", (int(loc_id),)
        ).fetchall()
        if not rows:
            return ""
        # Formatted 0 is the plain variant; fall back to stripping the markup.
        chosen = next((text for formatted, text in rows if formatted == 0), rows[0][1])
        return _MARKUP.sub("", chosen or "").strip()

    @lru_cache(maxsize=8192)
    def _card_row(self, grp_id: int) -> tuple | None:
        return self._connection.execute(
            "SELECT TitleId, TypeTextId, SubtypeTextId, Power, Toughness, ExpansionCode, Rarity "
            "FROM Cards WHERE GrpId = ?",
            (int(grp_id),),
        ).fetchone()

    def card_name(self, grp_id: int | None) -> str:
        if not grp_id:
            return ""
        row = self._card_row(int(grp_id))
        return self.localized(row[0]) if row else ""

    def type_line(self, grp_id: int | None) -> str:
        if not grp_id:
            return ""
        row = self._card_row(int(grp_id))
        if not row:
            return ""
        types = self.localized(row[1])
        subtypes = self.localized(row[2])
        return f"{types} — {subtypes}" if types and subtypes else types

    def printed_stats(self, grp_id: int | None) -> str:
        if not grp_id:
            return ""
        row = self._card_row(int(grp_id))
        if not row:
            return ""
        power, toughness = row[3], row[4]
        return f"{power}/{toughness}" if power not in (None, "") and toughness not in (None, "") else ""

    @lru_cache(maxsize=4096)
    def ability_text(self, ability_grp_id: int | None) -> str:
        if not ability_grp_id:
            return ""
        row = self._connection.execute(
            "SELECT TextId FROM Abilities WHERE Id = ?", (int(ability_grp_id),)
        ).fetchone()
        return self.localized(row[0]) if row else ""

    @lru_cache(maxsize=512)
    def prompt_text(self, prompt_id: int | None) -> str:
        if prompt_id is None:
            return ""
        row = self._connection.execute(
            "SELECT LocId FROM Prompts WHERE Id = ?", (int(prompt_id),)
        ).fetchone()
        text = self.localized(row[0]) if row else ""
        # Arena keeps retired prompt rows around; they are not user-facing text.
        return "" if text.startswith("#Deprecated") else text


_cached: tuple[str, float, CardDatabase] | None = None


def load_card_database() -> CardDatabase | None:
    """Return a shared CardDatabase, reopening it when Arena updates the file."""
    global _cached
    path = find_card_database()
    if path is None:
        return None
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    if _cached is not None:
        key, cached_stamp, database = _cached
        if key == str(path) and cached_stamp == stamp:
            return database
        database.close()
        _cached = None
    try:
        database = CardDatabase(path)
    except sqlite3.Error:
        return None
    _cached = (str(path), stamp, database)
    return database
