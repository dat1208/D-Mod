"""Pre-flight sanitisation before every LTK injection.

The LTK backend keeps state outside this process: LTK Manager, its injected
bridge DLL, the patcher host, a listening port, and a staging directory. When
a previous session is torn down badly (app killed, game crashed, machine
slept) any of those can survive as a zombie. The bridge then hand-picks the
leftover instance, believes the patcher is ready, and the next game launches
with no overlay at all -- which looks exactly like "the skin did not apply".

This module runs before each injection and forces a known-good starting point:

1. Kill orphaned `ltk_patcher_host.exe` / `ltk_patcher_host32.exe`.
2. Kill every `ltk-manager.exe` the bridge did not start (an ordinary instance
   carries no injected DLL, so the launcher refuses to start a second one).
3. Wait for the ports the bridge reserves to be released.
4. Report what was found so the log explains a failed injection.

Nothing here raises: a machine that is already clean is the common case, and a
cleanup that cannot complete must not stop the injection that follows.
"""
from __future__ import annotations

import os
import time
from typing import List, NamedTuple

import psutil

from utils.core.logging import get_logger

log = get_logger()

MANAGER_NAME = "ltk-manager.exe"
PATCHER_HOST_NAMES = ("ltk_patcher_host.exe", "ltk_patcher_host32.exe")
PORT_RANGE_START = 46321
PORT_RANGE_END = 46340

_KILL_TIMEOUT_S = 8.0
_PORT_RELEASE_TIMEOUT_S = 6.0


class PreflightReport(NamedTuple):
    """What the sanitisation found and did, for logging and tests."""

    orphan_hosts: int = 0
    ordinary_managers: int = 0
    ports_held: int = 0
    clean: bool = True

    def as_text(self) -> str:
        if self.clean:
            return "no stale LTK state"
        parts = []
        if self.orphan_hosts:
            parts.append(f"{self.orphan_hosts} orphan patcher host(s)")
        if self.ordinary_managers:
            parts.append(f"{self.ordinary_managers} ordinary LTK Manager instance(s)")
        if self.ports_held:
            parts.append(f"{self.ports_held} held bridge port(s)")
        return ", ".join(parts)


def _iter(paths, name: str):
    """Yield live processes whose executable name matches *name*.

    ``psutil.process_iter`` caches nothing, so a process that exits between the
    enumeration and the kill is simply absent. ``None`` yields are skipped
    because the tuple is only used for counting.
    """
    for proc in psutil.process_iter(["name", "pid", "exe", "create_time"]):
        info = proc.info or {}
        if (info.get("name") or "").lower() == name.lower():
            yield proc


def _kill(proc, timeout: float = _KILL_TIMEOUT_S) -> bool:
    """Terminate *proc* and wait for it to die; True when it is gone.

    ``terminate`` first so the manager can flush its library; ``kill`` only as
    a fallback, because a manager that dies mid-write leaves a half-written
    library.json behind.
    """
    pid = (proc.info or {}).get("pid")
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True
    except psutil.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=3.0)
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return True
        except psutil.TimeoutExpired:
            log.warning("[PREFLIGHT] Could not stop pid %s (still running)", pid)
            return False
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("[PREFLIGHT] Kill of pid %s failed: %s", pid, exc)
        return False


def kill_orphan_patcher_hosts() -> int:
    """Stop patcher hosts left behind by an earlier session.

    A host whose manager is gone can never be driven again -- the bridge
    addresses the host through the manager, not the host's own process. It
    also keeps the port and the staging lock that the next start needs.
    """
    stopped = 0
    for name in PATCHER_HOST_NAMES:
        for proc in _iter(None, name):
            if _kill(proc):
                stopped += 1
    return stopped


def kill_ordinary_managers(keep_pids=()) -> int:
    """Stop LTK Manager instances that the bridge did not start.

    *keep_pids* are the pids of the manager this app launched: those carry the
    injected DLL and must survive. Everything else is an ordinary instance the
    launcher would refuse to coexist with.
    """
    keep = {int(pid) for pid in keep_pids if pid}
    stopped = 0
    for proc in _iter(None, MANAGER_NAME):
        pid = (proc.info or {}).get("pid")
        if pid in keep:
            continue
        if _kill(proc):
            stopped += 1
    return stopped


def held_bridge_ports() -> List[int]:
    """Bridge ports that are currently listening, sorted.

    Read through psutil's own connection table rather than a shell call so the
    check works the same way inside a frozen build.
    """
    held: List[int] = []
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status != psutil.CONN_LISTEN:
                continue
            port = conn.laddr.port if conn.laddr else 0
            if PORT_RANGE_START <= port <= PORT_RANGE_END:
                held.append(port)
    except (psutil.AccessDenied, OSError) as exc:
        # Without elevation Windows can refuse the full table; the caller only
        # loses the warning, not the injection.
        log.debug("[PREFLIGHT] Could not read the connection table: %s", exc)
    return sorted(set(held))


def wait_ports_released(ports, timeout: float = _PORT_RELEASE_TIMEOUT_S) -> bool:
    """Block until none of *ports* is listening; False on timeout."""
    wanted = {int(port) for port in ports}
    if not wanted:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not (wanted & set(held_bridge_ports())):
            return True
        time.sleep(0.25)
    return False


def sanitize(keep_manager_pids=()) -> PreflightReport:
    """Force a known-good LTK starting point before an injection.

    Order matters: hosts first (they may be the ones holding a port), then
    managers, then the wait for the ports to actually drop. The ports are read
    before the kills so the report can say whether anything was held at all.
    """
    held_before = held_bridge_ports()
    orphan_hosts = kill_orphan_patcher_hosts()
    ordinary_managers = kill_ordinary_managers(keep_manager_pids)

    if held_before and (orphan_hosts or ordinary_managers):
        wait_ports_released(held_before)

    clean = not (held_before or orphan_hosts or ordinary_managers)
    report = PreflightReport(
        orphan_hosts=orphan_hosts,
        ordinary_managers=ordinary_managers,
        ports_held=len(held_before),
        clean=clean,
    )
    if clean:
        log.debug("[PREFLIGHT] %s", report.as_text())
    else:
        log.info("[PREFLIGHT] Cleaned up stale LTK state: %s", report.as_text())
    return report
