"""Is there a *newer* LTK release at all, and does it fix anything?

Why this exists
---------------
``patcher_update.refresh_patcher`` answers "is the installed DLL expired for
this game build?" and, if so, fetches a newer one. That question is about
time. It cannot say anything about the other failure mode: the DLL is not
expired, LTK itself is shipping a version that Vanguard blocks, and
re-downloading it changes nothing.

On 2026-11-25 that is exactly where D-PC sat. LTK Manager 1.33.0 displayed
"Vanguard update is blocking our patcher, we're working on a fix" while
``refresh_patcher`` found the staged 1.33.0 DLL, saw `_verify_staged_dll`
pass, saw the EOL date cover the game build, and reported the pair as
already good enough. Every check was true and the user still lost a match.

So this module answers a different question, and only when the patcher is
already known to be broken: **has upstream published anything since the
version installed here?** Three outcomes, all useful before a match:

``update_available``
    A newer release exists. Updating is worth doing.

``up_to_date``
    Upstream's newest release is the one already installed. The block is
    theirs to fix; nothing the user does locally will help.

``unknown``
    GitHub could not be reached or answered something unexpected. Say so
    rather than guess -- a wrong "up_to_date" would read as "abandon hope".
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from utils.core.logging import get_logger
from utils.core.paths import get_state_dir

log = get_logger(__name__)

STATE_NAME = "patcher_release.json"
SCHEMA = 1

RELEASE_API = "https://api.github.com/repos/LeagueToolkit/ltk-manager/releases/latest"

# GitHub's unauthenticated limit is 60 requests an hour per IP. The UI polls
# /api/status every two seconds, so a single unanswered check must not become
# thousands of requests: one lookup per this window, cached on disk.
CACHE_TTL = timedelta(hours=6)

STATUS_AVAILABLE = "update_available"
STATUS_CURRENT = "up_to_date"
STATUS_UNKNOWN = "unknown"

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def state_file() -> Path:
    return get_state_dir() / STATE_NAME


def _as_tuple(text: str) -> Optional[tuple]:
    match = _VERSION_RE.search(text or "")
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def installed_version(manager_exe: Optional[Path]) -> Optional[str]:
    """The version LTK Manager reports for itself, e.g. 1.33.0.

    Read from the file's version resource -- the same number the app prints
    in its title bar. None when the exe is missing or on a non-Windows host,
    which makes the comparison impossible rather than wrong.
    """
    if manager_exe is None or not Path(manager_exe).is_file():
        return None
    import sys
    if sys.platform != "win32":
        return None
    import subprocess
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-Item -LiteralPath $env:DCHANGER_PROBE).VersionInfo.FileVersion"],
            env={**__import__("os").environ, "DCHANGER_PROBE": str(manager_exe)},
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("[PATCHER-RELEASE] could not read the installed version (%s)", exc)
        return None
    text = (result.stdout or "").strip()
    return text or None


def _read_cache() -> dict:
    path = state_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("schema") == SCHEMA else {}


def _write_cache(data: dict) -> None:
    try:
        path = state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    except OSError as exc:
        log.warning("[PATCHER-RELEASE] could not save the cache (%s)", exc)


def _fresh(entry: dict) -> bool:
    stamp = entry.get("checked_at")
    if not stamp:
        return False
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when < CACHE_TTL


def _fetch_latest() -> Optional[str]:
    """The newest published release tag, or None when GitHub cannot be read."""
    import requests

    from config import APP_USER_AGENT

    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": APP_USER_AGENT,
            "Accept": "application/vnd.github+json",
        })
        response = session.get(RELEASE_API, timeout=20)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - any failure means "unknown"
        log.warning("[PATCHER-RELEASE] GitHub lookup failed (%s)", exc)
        return None
    tag = payload.get("tag_name") or payload.get("name")
    return str(tag).strip() if tag else None


def check(manager_exe: Optional[Path] = None, *, force: bool = False) -> dict:
    """Compare the installed LTK Manager with the newest published release.

    Never raises. Returns a dict with: status, installed, latest, detail.
    ``force`` skips the on-disk cache, for a user asking directly.
    """
    installed = installed_version(manager_exe)
    if installed is None:
        return {"status": STATUS_UNKNOWN, "installed": None, "latest": None,
                "detail": "LTK Manager is not installed, or its version "
                          "could not be read"}

    cache = _read_cache()
    entry = cache.get("release") or {}
    if not force and _fresh(entry) and entry.get("installed") == installed:
        latest = entry.get("latest")
    else:
        latest = _fetch_latest()
        if latest is None:
            return {"status": STATUS_UNKNOWN, "installed": installed,
                    "latest": None,
                    "detail": "GitHub could not be reached; the release "
                              "check is unavailable right now"}
        _write_cache({"schema": SCHEMA, "release": {
            "installed": installed,
            "latest": latest,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }})

    mine, theirs = _as_tuple(installed), _as_tuple(latest)
    if mine is None or theirs is None:
        return {"status": STATUS_UNKNOWN, "installed": installed,
                "latest": latest,
                "detail": "the version strings could not be compared"}

    if theirs > mine:
        return {"status": STATUS_AVAILABLE, "installed": installed,
                "latest": latest,
                "detail": "LTK %s is published; this machine runs %s -- "
                          "updating may lift the block" % (latest, installed)}

    return {"status": STATUS_CURRENT, "installed": installed, "latest": latest,
            "detail": "LTK %s is the newest release; the block is upstream's "
                      "to fix and no local update will change it" % latest}
