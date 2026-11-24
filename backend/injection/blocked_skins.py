"""Persistent record of skins LTK Manager refused, keyed by archive digest.

Why this exists
---------------
Injection happens before the game starts, so a refusal is only observable
*after* the overlay was built and the player already queued. The picker
cannot warn in time, and a refusal the second time is a wasted game.

So the refusal itself becomes the evidence: when LTK reports ``c0000229``
for the archive D-Mod just handed it, that archive is known bad on this
machine, against this game build. Nothing is guessed. The next time the
same archive is offered, the picker locks the skin before the player can
pick it.

What is recorded
----------------
``sha256`` of the archive. Content, not path: the library is re-downloaded
in place, so a path can point at a repaired archive while its digest moves.
A stored record also names the game version it was refused under, because a
patch can fix a skin as easily as it can break one.

Storage
-------
``<state>/blocked_skins.json`` next to the other app state, written through
``write_text_atomic`` so a crash mid-write cannot leave a truncated list.
Unreadable state is treated as empty: a broken file must never be able to
lock every skin.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Dict, Optional

from utils.core.atomic_file import write_text_atomic
from utils.core.logging import get_logger
from utils.core.paths import get_state_dir

log = get_logger()

STATE_NAME = "blocked_skins.json"

# LTK's code for "the skinhack rule refused this archive". Kept as a string
# so a code from a future manager version can still be stored.
SKINHACK_CODE = "c0000229"


def state_file() -> Path:
    return get_state_dir() / STATE_NAME


def archive_digest(archive) -> Optional[str]:
    """sha256 of the archive bytes, or None when it cannot be read."""
    path = Path(archive)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        log.debug("[BLOCKLIST] Could not hash %s: %s", path, exc)
        return None
    return digest.hexdigest()


def _read_state() -> Dict[str, dict]:
    """Every recorded refusal, keyed by digest. Never raises."""
    path = state_file()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("[BLOCKLIST] Ignoring unreadable %s: %s", path, exc)
        return {}
    if not isinstance(payload, dict):
        return {}
    entries = payload.get("skins")
    if not isinstance(entries, dict):
        return {}
    return {k: v for k, v in entries.items() if isinstance(v, dict)}


def _write_state(entries: Dict[str, dict]) -> None:
    path = state_file()
    if not entries:
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            log.warning("[BLOCKLIST] Could not clear %s: %s", path, exc)
        return
    write_text_atomic(path, json.dumps({"skins": entries}, indent=2, sort_keys=True))


def record_refusal(archive, *, code: str = SKINHACK_CODE, reason: str = "",
                   game_version: Optional[str] = None,
                   champion: Optional[str] = None,
                   skin_id: Optional[int] = None) -> Optional[str]:
    """Remember that LTK refused *archive*. Returns the digest it stored under.

    Called only after LTK reported the refusal in its own log; this is the
    transcription of a verdict, not a prediction.
    """
    digest = archive_digest(archive)
    if digest is None:
        return None
    entries = _read_state()
    previous = entries.get(digest) or {}
    entries[digest] = {
        "code": str(code or SKINHACK_CODE),
        "reason": str(reason or previous.get("reason") or ""),
        "gameVersion": str(game_version or previous.get("gameVersion") or ""),
        "champion": str(champion or previous.get("champion") or ""),
        "skinId": skin_id if skin_id is not None else previous.get("skinId"),
        "path": str(Path(archive)),
        "recordedAt": int(time.time()),
        "timesRefused": int(previous.get("timesRefused") or 0) + 1,
    }
    _write_state(entries)
    log.info("[BLOCKLIST] Recorded LTK refusal %s for %s (digest %s)",
             entries[digest]["code"], Path(archive).name, digest[:12])
    return digest


def is_blocked(archive, game_version: Optional[str] = None) -> Optional[dict]:
    """The stored refusal for *archive*, or None when it is not known bad.

    When *game_version* is given, a record from another game build is
    ignored: the patch that changed the game may also have fixed the skin,
    and locking a skin forever on an old verdict is worse than re-testing it.
    """
    digest = archive_digest(archive)
    if digest is None:
        return None
    record = _read_state().get(digest)
    if not record:
        return None
    recorded_version = str(record.get("gameVersion") or "")
    if game_version and recorded_version and recorded_version != str(game_version):
        log.debug("[BLOCKLIST] Ignoring %s refusal: recorded under %s, now %s",
                  Path(archive).name, recorded_version, game_version)
        return None
    return record


def forget(archive) -> bool:
    """Drop *archive* from the list (after a successful selection)."""
    digest = archive_digest(archive)
    if digest is None:
        return False
    entries = _read_state()
    if digest not in entries:
        return False
    entries.pop(digest)
    _write_state(entries)
    log.info("[BLOCKLIST] Cleared refusal for %s", Path(archive).name)
    return True


def all_blocked() -> Dict[str, dict]:
    """Every recorded refusal, keyed by digest."""
    return _read_state()
