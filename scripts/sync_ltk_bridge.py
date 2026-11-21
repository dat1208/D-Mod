#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Copy the LibreToolkit (LTF) bridge into the backend bundle.

D-Changer drives the official LTK Manager through the authenticated loopback
bridge from the LibreToolkit project: ltk_launcher.exe loads LeagueToolFlop.dll
into ltk-manager.exe, and the DLL then serves the local JSON API. Those two
files are third-party binaries; this script copies them unmodified from an
installed Rose build (or a path given with --source) so the build script can
bundle them next to dchanger-backend.exe. It patches nothing it copies.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

FILES = ('ltk_launcher.exe', 'LeagueToolFlop.dll')
SOURCES = (
    Path(r'C:\Program Files\Rose\_internal\injection\tools'),
    Path(r'C:\Program Files (x86)\Rose\_internal\injection\tools'),
)
ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / 'backend' / 'ltk_bridge'


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def find_source(explicit):
    candidates = [Path(explicit)] if explicit else list(SOURCES)
    for candidate in candidates:
        if candidate.is_dir() and all((candidate / name).is_file() for name in FILES):
            return candidate
    raise SystemExit('LTF bridge source not found; pass --source <Rose tools dir>')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', help='Rose injection tools directory')
    args = parser.parse_args(argv)
    source = find_source(args.source)
    TARGET.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        src = source / name
        dst = TARGET / name
        shutil.copy2(src, dst)
        print('%s: %s -> %s' % (name, digest(src), dst))
    print('Synced %d files from %s (unmodified)' % (len(FILES), source))
    return 0


if __name__ == '__main__':
    sys.exit(main())
