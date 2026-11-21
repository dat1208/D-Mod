"""Explicit, safe handoff of an archived skin to the official LTK Manager UI.

LTK Manager accepts mod files on its command line, but has no documented
external API to enable a mod or start its patcher. Therefore this bridge NEVER
claims to have patched the game: the user must enable the mod and press Start.
It does not change LTK Manager's library.json or attempt to inject into it.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from .classic import to_classic_champion_id, to_library_id, to_library_skin_name
from .mods.zip_resolver import ZipResolver

_SKIN = re.compile(r"^(?:skin|chroma)_(\d+)$")


def resolve_skin_archive(skins_dir: Path, skin_name: str, champion_id: int,
                         chroma_id: Optional[int] = None, *, classic: bool = False,
                         classic_dir: Optional[Path] = None) -> Path:
    if not isinstance(skin_name, str) or not _SKIN.fullmatch(skin_name):
        raise ValueError("a numeric skin_ or chroma_ ID is required")
    if not isinstance(champion_id, int) or champion_id <= 0:
        raise ValueError("champion ID is required")
    root = Path(skins_dir)
    if classic:
        if classic_dir is None:
            raise ValueError("Classic library path is required")
        champion_id = to_classic_champion_id(champion_id)
        root = Path(classic_dir)
        skin_name = to_library_skin_name(root / str(champion_id), skin_name)
        if chroma_id is not None:
            chroma_id = to_library_id(root / str(champion_id), chroma_id)
    resolved = ZipResolver(root).resolve_zip(skin_name, chroma_id, skin_name,
                                            champion_id=champion_id)
    if not resolved or not resolved.is_file() or resolved.suffix.lower() not in (".fantome", ".modpkg"):
        raise FileNotFoundError(f"LTK-compatible archive for {skin_name} is unavailable")
    if not resolved.resolve().is_relative_to(root.resolve()):
        raise ValueError("skin archive must be inside the configured library")
    return resolved


def find_manager(candidates=None) -> Path:
    candidates = candidates or (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "LTK Manager" / "ltk-manager.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "LTK Manager" / "ltk-manager.exe",
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return Path(candidate)
    raise FileNotFoundError("Official LTK Manager is not installed in a standard location")


def handoff(archive: Path, manager: Path, *, spawn=subprocess.Popen) -> None:
    archive, manager = Path(archive), Path(manager)
    if not archive.is_file() or archive.suffix.lower() not in (".fantome", ".modpkg"):
        raise ValueError("LTK requires an existing .fantome or .modpkg file")
    if not manager.is_file() or manager.name.lower() != "ltk-manager.exe":
        raise ValueError("LTK Manager executable is missing")
    # LTK's files.rs accepts a mod-file argument. It opens the import UI; it
    # does not auto-toggle the mod or start the patcher.
    spawn([str(manager), str(archive)], cwd=str(manager.parent),
          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
