"""Explicit one-shot handoff to LTK Manager; never starts a second patcher.

Example (after closing Rose/D-Changer):
  python scripts/handoff_ltk_skin.py skin_163011 163 --manager "C:\\Program Files\\LTK Manager\\ltk-manager.exe"

LTK Manager opens its import UI. Enable the imported mod and press Start there.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from injection.ltk_handoff import find_manager, handoff, resolve_skin_archive
from utils.core.paths import get_classic_skins_dir, get_skins_dir


def main() -> None:
    ap = argparse.ArgumentParser(description="Open a skin archive in the official LTK Manager UI")
    ap.add_argument("skin_id", help="skin_<number> or chroma_<number>")
    ap.add_argument("champion_id", type=int)
    ap.add_argument("--manager", type=Path, help="Path to an installed ltk-manager.exe")
    ap.add_argument("--classic", action="store_true")
    ap.add_argument("--chroma", type=int)
    args = ap.parse_args()
    archive = resolve_skin_archive(get_skins_dir(), args.skin_id, args.champion_id,
                                   args.chroma, classic=args.classic,
                                   classic_dir=get_classic_skins_dir())
    exe = args.manager if args.manager else find_manager()
    handoff(archive, exe)
    print(f"LTK Manager import requested: {archive}")
    print("This does NOT enable the mod or start patching. Check the manager UI; press Start there.")


if __name__ == "__main__":
    main()
