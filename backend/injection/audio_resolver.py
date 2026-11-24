"""Report whether a .fantome keeps its skill audio, without touching it.

Rose's packer never stored the skin SFX bank inside the archive.  Instead it
wrote META/rose_sounds.json, a list of copies the Rose tool performed at
runtime:

    {"copies": [{"from": "ASSETS/.../Skin49/Zed_Skin49_SFX_audio.bnk",
                 "to":   "ASSETS/.../Base/Zed_Base_SFX_audio.bnk",
                 "wad":  "Zed"}]}

LTK Manager overlays static files only, so it copies nothing.  The archive's
Zed_Base_SFX_audio.bnk (the destination) gets overlaid while the matching
events bank stays at its base version, and the skin ends up silent.

Evidence (D-PC, 14 Zed skins):

    13/14 declare copies whose "from" bank is absent from the WAD
    238002 writes no rose_sounds.json and plays correctly

This module only reads.  It never rewrites a WAD and never patches the game:
a missing source bank cannot be invented, and a half-written bank is worse
than a silent skin.
"""
from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from utils.core.logging import get_logger

log = get_logger()

SUFFIXES = (".fantome", ".modpkg", ".zip")
SOUNDS_MEMBER = "META/rose_sounds.json"
_READ_LIMIT = 24 * 1024 * 1024

# Verdicts the UI and the Pengu plugin switch on.
AUDIO_OK = "ok"
AUDIO_MISSING_SRC = "missing_src"
AUDIO_UNKNOWN = "unknown"
AUDIO_FALLBACK = "fallback"  # copy is dead, but the target bank still plays


@dataclass(frozen=True)
class AudioVerdict:
    """Whether this archive can carry its own skill audio."""

    archive: str
    status: str
    champion: Optional[str] = None
    copies: int = 0
    missing: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.status == AUDIO_OK

    @property
    def summary(self) -> str:
        if self.status == AUDIO_OK:
            return "Skill audio is intact"
        if self.status == AUDIO_UNKNOWN:
            return "Could not read this archive; audio is unverified"
        names = ", ".join(self.missing)
        if self.status == AUDIO_FALLBACK:
            return (
                "This skin keeps the default champion sounds: the archive "
                "declares an audio copy whose source bank is missing ("
                + names
                + "), but the bank it copies onto is still in the WAD, so "
                "nothing goes silent."
            )
        return (
            "This skin will lose its skill sounds: the archive declares "
            + str(self.copies)
            + " audio copy/copies and the source bank is missing ("
            + names
            + "). LTK overlays files present in the WAD and cannot run Rose's "
            "runtime copy."
        )

    def as_dict(self) -> Dict[str, object]:
        return {
            "code": {
                AUDIO_MISSING_SRC: "no_audio",
                AUDIO_FALLBACK: "audio_fallback",
            }.get(self.status, self.status),
            "status": self.status,
            "archive": self.archive,
            "champion": self.champion,
            "copies": self.copies,
            "missing": list(self.missing),
            "message": self.summary,
        }


def _wad_members(names: Iterable[str]) -> List[str]:
    return sorted(n for n in names if n.lower().endswith(".wad.client"))


def _member_for_wad(members: List[str], wad: Optional[str]) -> Optional[str]:
    """Pick the WAD a copy names, else the champion-sized one."""
    if wad:
        wanted = (wad.strip().lower() + ".wad.client")
        for member in members:
            if member.replace("\\", "/").rsplit("/", 1)[-1].lower() == wanted:
                return member
    return members[0] if members else None


def _bank_present(payload: bytes, path: str) -> bool:
    """Whether a WAD slice names *path* at all.

    The WAD keeps its path table as plain ASCII, so a substring search is
    enough: parsing the container would add a dependency for no extra signal.
    """
    needle = path.replace("\\", "/").encode("ascii", "ignore")
    return needle in payload


def _champion_of(member: Optional[str]) -> Optional[str]:
    if not member:
        return None
    stem = member.replace("\\", "/").rsplit("/", 1)[-1]
    if stem.lower().endswith(".wad.client"):
        stem = stem[: -len(".wad.client")]
    return stem.strip().lower() or None


def inspect_archive(archive) -> AudioVerdict:
    """Classify *archive* by whether its declared audio sources exist.

    Read-only and exception-safe: an archive that cannot be read is reported
    as unknown rather than blocked.
    """
    archive = Path(archive)
    if not archive.is_file() or archive.suffix.lower() not in SUFFIXES:
        return AudioVerdict(archive=str(archive), status=AUDIO_UNKNOWN)

    try:
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            if SOUNDS_MEMBER not in names:
                # LTK-native: no runtime copy, so nothing can go missing.
                member = (_wad_members(names) or [None])[0]
                return AudioVerdict(
                    archive=str(archive),
                    status=AUDIO_OK,
                    champion=_champion_of(member),
                )

            try:
                declared = json.loads(bundle.read(SOUNDS_MEMBER).decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                log.debug(
                    "[AUDIO] Unreadable rose_sounds.json in "
                    + archive.name
                    + ": "
                    + str(exc)
                )
                return AudioVerdict(archive=str(archive), status=AUDIO_UNKNOWN)

            copies = declared.get("copies") or []
            if not isinstance(copies, list) or not copies:
                member = (_wad_members(names) or [None])[0]
                return AudioVerdict(
                    archive=str(archive),
                    status=AUDIO_OK,
                    champion=_champion_of(member),
                )

            members = _wad_members(names)
            champion: Optional[str] = None
            missing: List[str] = []
            silenced: List[str] = []
            rescued: List[str] = []
            for entry in copies:
                if not isinstance(entry, dict):
                    continue
                source = entry.get("from")
                if not isinstance(source, str) or not source.strip():
                    continue
                member = _member_for_wad(members, entry.get("wad"))
                if member is None:
                    missing.append(source)
                    continue
                champion = champion or _champion_of(member)
                with bundle.open(member) as handle:
                    payload = handle.read(_READ_LIMIT)
                if _bank_present(payload, source):
                    continue
                # The copy cannot run. Whether the skin goes silent depends on
                # one thing: is a bank it could still play already in the WAD?
                # Rose's copies target the skin's own bank slot, and skins ship
                # that slot filled (Base/Base2). A dead copy therefore leaves a
                # playable bank behind, not a hole.
                target = entry.get("to")
                if isinstance(target, str) and target.strip() and _bank_present(payload, target):
                    rescued.append(source)
                else:
                    silenced.append(source)

            if silenced:
                missing.extend(silenced)
            elif rescued:
                verdict = AudioVerdict(
                    archive=str(archive),
                    status=AUDIO_FALLBACK,
                    champion=champion,
                    copies=len(copies),
                    missing=tuple(rescued),
                )
                log.info("[AUDIO] " + verdict.summary)
                return verdict

            if missing:
                verdict = AudioVerdict(
                    archive=str(archive),
                    status=AUDIO_MISSING_SRC,
                    champion=champion,
                    copies=len(copies),
                    missing=tuple(missing),
                )
                log.warning("[AUDIO] " + verdict.summary)
                return verdict

            return AudioVerdict(
                archive=str(archive),
                status=AUDIO_OK,
                champion=champion,
                copies=len(copies),
            )
    except (OSError, zipfile.BadZipFile, RuntimeError, EOFError) as exc:
        log.debug("[AUDIO] Could not inspect " + archive.name + ": " + str(exc))
        return AudioVerdict(archive=str(archive), status=AUDIO_UNKNOWN)


def inspect_archives(paths: Iterable) -> Dict[str, Dict[str, object]]:
    """Inspect many archives; the shape the API route returns."""
    verdicts = {}
    for path in paths:
        verdict = inspect_archive(path)
        if verdict.status != AUDIO_OK:
            verdicts[str(path)] = verdict.as_dict()
    return verdicts
