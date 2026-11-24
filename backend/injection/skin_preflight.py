"""Predict and report LTK skinhack verdict (c0000229) before injection.

LTK Manager refuses some legitimate skins: its bin_skinhack rule reads
data/characters/CHAMP/skins/skin0.bin inside the archive WAD and reports
SkinhackDetected (c0000229) as soon as an audio event or bank name matches
CHAMPSkinN with N not 0.

Evidence (D-PC, LTK Manager 1.33.0, patch 16.23.8406595):

    zed 238013  Play_sfx_ZedSkin13Shadow_Joke3D_*  -> c0000229
    zed 238068  Play_sfx_ZedSkin68Shadow_Joke3D_*  -> c0000229
    zed 238002  no ZedSkinN                       -> overlays fine
    zed 238010  no ZedSkinN                       -> overlays fine

Same pattern upstream: LeagueSkins 185, LeagueSkins 193, LTK 935, LTK 965.
The rule lives in a closed-source DLL, so a warning is the only honest
thing the app can do before the game starts.

How the scan works: a .fantome carries WAD/NAME.wad.client, a binary "RW"
container whose TOC and file table keep path strings and bank/event names
as plain ASCII at the head of the archive. skin0.bin is never a separate
zip entry, so the whole WAD is scanned for CHAMPSkinN tokens instead of
parsing the container: no external dependency, no decompression.

Offline and read-only: opening a .fantome/.modpkg is a zip read, so the
check works before LTK Manager runs, before ChampSelect, before a game.
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from utils.core.logging import get_logger

log = get_logger()

SUFFIXES = (".fantome", ".modpkg", ".zip")
WAD_SUFFIX = ".wad.client"

# CHAMPSkinN where N is 1..9999. Anchored on letters so that skin0 and
# Skin0 (the base skin) never match: only N not 0 is a skinhack signal.
TOKEN = re.compile(rb"[A-Za-z]{2,20}[Ss]kin_?([1-9][0-9]{0,3})(?![0-9])")

# How much of each WAD to read. Paths, TOC and the audio event names of
# skin0.bin all sit in the first few MB; the 2.5 GB map WADs are not
# touched because only *.wad.client members are opened, and reading stops
# once a verdict is known.
_READ_LIMIT = 24 * 1024 * 1024


@dataclass(frozen=True)
class SkinhackWarning:
    """Why LTK is expected to refuse this archive."""

    archive: str
    champion: Optional[str] = None
    skin_numbers: Tuple[int, ...] = field(default_factory=tuple)
    events: Tuple[str, ...] = field(default_factory=tuple)
    member: Optional[str] = None

    @property
    def summary(self) -> str:
        nums = ", ".join(str(n) for n in self.skin_numbers)
        return (
            "LTK will refuse this skin (c0000229): its WAD declares "
            "audio for skin " + nums + ", which the skinhack rule treats "
            "as a paid-skin audio copy")

    def as_dict(self) -> Dict[str, object]:
        return {
            "code": "c0000229",
            "status": "SkinhackDetected",
            "archive": self.archive,
            "champion": self.champion,
            "skin_numbers": list(self.skin_numbers),
            "events": list(self.events),
            "member": self.member,
            "message": self.summary,
        }


def _wad_members(names: Iterable[str]) -> List[str]:
    """Every *.wad.client the archive carries, champion WADs included."""
    return sorted(n for n in names if n.lower().endswith(WAD_SUFFIX))


def _champion_slug(member: str) -> Optional[str]:
    """Zed.wad.client -> zed."""
    stem = member.replace(chr(92), "/").rsplit("/", 1)[-1]
    if stem.lower().endswith(WAD_SUFFIX):
        stem = stem[: -len(WAD_SUFFIX)]
    slug = stem.strip().lower()
    return slug or None


def _decode(payload: bytes, slug: Optional[str]) -> Tuple[Tuple[int, ...], Tuple[str, ...]]:
    """Pull CHAMPSkinN tokens out of a WAD slice."""
    numbers, events = set(), set()
    for match in TOKEN.finditer(payload):
        token = match.group(0).decode("ascii", "replace")
        if slug and slug not in token.lower():
            continue
        try:
            number = int(match.group(1))
        except ValueError:
            continue
        if number == 0:
            continue
        numbers.add(number)
        events.add(token)
    return tuple(sorted(numbers)), tuple(sorted(events))


def scan_archive(archive) -> Optional[SkinhackWarning]:
    """Return a warning when this archive is expected to fail, else None.

    Read-only and exception-safe: an unreadable archive returns None rather
    than blocking injection. A skin that cannot be inspected is not a skin
    that is known to be bad.
    """
    archive = Path(archive)
    if not archive.is_file():
        return None
    if archive.suffix.lower() not in SUFFIXES:
        return None
    try:
        with zipfile.ZipFile(archive) as bundle:
            for member in _wad_members(bundle.namelist()):
                slug = _champion_slug(member)
                with bundle.open(member) as handle:
                    payload = handle.read(_READ_LIMIT)
                numbers, events = _decode(payload, slug)
                if numbers:
                    warning = SkinhackWarning(
                        archive=str(archive),
                        champion=slug,
                        skin_numbers=numbers,
                        events=events,
                        member=member,
                    )
                    log.warning("[PREFLIGHT] " + warning.summary)
                    return warning
    except (OSError, zipfile.BadZipFile, RuntimeError, EOFError) as exc:
        log.debug("[PREFLIGHT] Could not inspect " + archive.name + ": " + str(exc))
        return None
    return None


def scan_archives(paths: Iterable) -> Dict[str, Dict[str, object]]:
    """Scan many archives; the shape the API route returns."""
    warnings = {}
    for path in paths:
        warning = scan_archive(path)
        if warning is not None:
            warnings[str(path)] = warning.as_dict()
    return warnings
