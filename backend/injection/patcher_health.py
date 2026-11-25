"""Is the LTK patcher actually able to inject into this build of the game?

Why this exists
---------------
On 2026-11-25 Riot shipped a Vanguard update (vgk.sys dated 00:10) that
blocks the LTK patcher DLL from loading into the game process. LTK Manager
1.33.0 says so itself, in a banner on its home screen:

    "Vanguard update is blocking our patcher, we're working on a fix"

The failure is silent from the outside. The host still reports
``hook installed``, the overlay builds fine, the game starts and ends
``clean, no incident recorded`` -- but the DLL never writes a single
``[ltk-dll]`` line, because it never got into the process. The player finds
out the hard way: a match is played with the stock skin.

What is measurable, and what is not
-----------------------------------
There is no API that answers "can you inject right now". check_mod_health
reports healthy for archives the patcher later refuses, and the patcher has
no dry run. So this module does not predict -- it observes, and it remembers:

patched_ok
    LTK's log shows the DLL running in a game process for this build
    ([ltk-dll ...] redirected wad, or the base-skin refusal). The patcher
    demonstrably reached the game.

dll_absent
    A game was hooked (hook installed) and the log holds no [ltk-dll] line
    from that process. The DLL did not load. This is the Vanguard case.

The verdict is keyed by game build, so a League patch -- or an LTK fix --
re-opens the question and the check runs again. It stays bad only for the
build that produced it, so the player is warned before queueing instead of
after.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils.core.logging import get_logger
from utils.core.paths import get_state_dir

log = get_logger(__name__)

STATE_NAME = "patcher_health.json"
SCHEMA = 1

# A host that announces a game and prints no DLL line within this many lines
# of the hook is the silent-block signature. The window is generous because
# verbose logging interleaves build notices with host output.
_WINDOW_LINES = 400

_HOOK_RE = re.compile(r"hook installed tid=(?P<tid>\d+) pid=(?P<pid>\d+)")
_DLL_RE = re.compile(r"\[ltk-dll\b")
_BUILD_RE = re.compile(r'game_build=Some\("(?P<build>[^"]+)"\)')
_MANAGER_LOG_RE = re.compile(r"^ltk-manager\.(?P<date>\d{4}-\d{2}-\d{2})\.log$")

VERDICT_OK = "patched_ok"
VERDICT_DLL_ABSENT = "dll_absent"


def state_file() -> Path:
    return get_state_dir() / STATE_NAME


def _read_state() -> dict:
    path = state_file()
    if not path.is_file():
        return {"schema": SCHEMA, "builds": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("[PATCHER-HEALTH] ignoring unreadable state (%s)", exc)
        return {"schema": SCHEMA, "builds": {}}
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return {"schema": SCHEMA, "builds": {}}
    if not isinstance(data.get("builds"), dict):
        data["builds"] = {}
    return data


def _write_state(data: dict) -> None:
    path = state_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        path.write_text(payload, encoding="utf-8")
    except OSError as exc:
        log.warning("[PATCHER-HEALTH] could not save state (%s)", exc)


def newest_manager_log(roaming: Optional[Path] = None) -> Optional[Path]:
    """The most recent ltk-manager*.log, or None when LTK never ran."""
    if roaming is None:
        import os
        roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    directory = Path(roaming) / "dev.leaguetoolkit.manager" / "logs"
    if not directory.is_dir():
        return None
    best = None
    best_key = ""
    for entry in directory.iterdir():
        match = _MANAGER_LOG_RE.match(entry.name)
        if not match or not entry.is_file():
            continue
        if match.group("date") > best_key:
            best_key = match.group("date")
            best = entry
    return best


def game_build(log_path: Path) -> Optional[str]:
    """The build the manager last built the overlay for, e.g. 16.23.8418428."""
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-4000:]
    except OSError:
        return None
    found = None
    for line in lines:
        match = _BUILD_RE.search(line)
        if match:
            found = match.group("build")
    return found


def classify_log(log_path: Path) -> Optional[tuple]:
    """(verdict, detail) for the last game this log saw, or None.

    None means the log has no game at all -- the question is still open, and
    writing a verdict would invent evidence.
    """
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-8000:]
    except OSError:
        return None

    hook_at = None
    hook_pid = None
    for index, line in enumerate(lines):
        match = _HOOK_RE.search(line)
        if match:
            hook_at = index
            hook_pid = match.group("pid")

    if hook_at is None:
        return None

    # Everything the DLL printed *after* the last hook belongs to that process.
    tail = lines[hook_at:hook_at + _WINDOW_LINES]
    for line in tail:
        if _DLL_RE.search(line):
            return (VERDICT_OK,
                    "LTK's DLL reached the game (pid %s)" % hook_pid)

    return (VERDICT_DLL_ABSENT,
            "Game pid %s was hooked but LTK's DLL never loaded; "
            "a Vanguard update blocks it (LTK 1.33.0 confirms)" % hook_pid)


def record(build: Optional[str], verdict: str, detail: str) -> dict:
    """Persist one observation for one game build."""
    if not build:
        return {"build": None, "verdict": verdict, "detail": detail}
    data = _read_state()
    data["builds"][build] = {
        "verdict": verdict,
        "detail": detail,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_state(data)
    log.info("[PATCHER-HEALTH] %s for build %s", verdict, build)
    return data["builds"][build]


def check(roaming: Optional[Path] = None) -> dict:
    """Observe the newest LTK log and return the verdict for this build.

    Returns a dict with: build, verdict (patched_ok / dll_absent / unknown),
    detail, blocking (bool). ``blocking`` is True only for a measured
    dll_absent -- never for "no evidence yet", so a fresh install does not
    warn before anything has been tried.
    """
    log_path = newest_manager_log(roaming)
    if log_path is None:
        return {"build": None, "verdict": "unknown",
                "detail": "LTK Manager has not run yet",
                "blocking": False}

    build = game_build(log_path)
    observed = classify_log(log_path)
    if observed is None:
        return {"build": build, "verdict": "unknown",
                "detail": "No game has run since LTK started",
                "blocking": False}

    verdict, detail = observed
    record(build, verdict, detail)
    return {"build": build, "verdict": verdict, "detail": detail,
            "blocking": verdict == VERDICT_DLL_ABSENT}


def verdict_for(build: Optional[str]) -> Optional[dict]:
    """The stored verdict for *build*, or None."""
    if not build:
        return None
    entry = _read_state()["builds"].get(build)
    return dict(entry) if isinstance(entry, dict) else None


def is_blocking(build: Optional[str] = None) -> bool:
    """True when this build is known to swallow the DLL."""
    if build:
        entry = verdict_for(build)
        return bool(entry and entry.get("verdict") == VERDICT_DLL_ABSENT)
    return check().get("blocking", False)
