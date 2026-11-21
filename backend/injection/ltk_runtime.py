"""LTK Manager runtime via the authenticated local LTF bridge.

Opt-in with [General] injection_backend = ltk. Never starts a second host;
never writes LTK Manager's library.json. The bridge is LeagueToolFlop
(`ltk_launcher.exe` + `LeagueToolFlop.dll`), a third-party LibreToolkit
driver shipped inside this build under `ltk_bridge/`; no Rose installation
is consulted. The manager must be closed before starting the bridge (an
existing ordinary instance cannot be injected safely).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import psutil

from utils.core.logging import get_logger
from .tools.bridge import LtkBridge, BridgeCommandError, BridgeUnavailable, find_bridge

log = get_logger()
_IMPORT_TIMEOUT = 55
_PATCHER_TIMEOUT = 90
_MANAGER_PATHS = tuple(p for p in (
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "LTK Manager" / "ltk-manager.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "LTK Manager Probe2" / "ltk-manager.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "LTK Manager" / "ltk-manager.exe",
) if str(p))
def _bundled_bridge_dir():
    """The LTF bridge shipped with a frozen build.

    PyInstaller 6 unpacks bundled data into ``_internal`` next to the exe;
    older one-directory builds put it in the exe directory. Check both.
    """
    if not getattr(sys, "frozen", False):
        return None
    root = Path(os.path.abspath(sys.executable)).parent
    for candidate in (root / "ltk_bridge", root / "_internal" / "ltk_bridge"):
        if (candidate / "ltk_launcher.exe").is_file():
            return candidate
    return None


_BRIDGE_PATHS = tuple(p for p in (_bundled_bridge_dir(),) if p is not None)
_SKIN_NAME = re.compile(r"^(skin|chroma)_(\d+)$")


class LtkRuntimeError(RuntimeError):
    pass


def _running(name: str) -> bool:
    return any(proc.info.get("name", "").lower() == name.lower()
               for proc in psutil.process_iter(["name"]))


def _get_manager() -> Path:
    from config import get_config_option
    custom = get_config_option("General", "ltkManagerPath")
    paths = ((Path(custom),) if custom else ()) + _MANAGER_PATHS
    for path in paths:
        if path.is_file():
            return path
    raise LtkRuntimeError("LTK Manager not found; set General.ltkManagerPath")


def _get_launcher() -> Path:
    from config import get_config_option
    custom = get_config_option("General", "ltfToolsPath")
    directories = ((Path(custom),) if custom else ()) + _BRIDGE_PATHS
    for directory in directories:
        exe = directory / "ltk_launcher.exe"
        if exe.is_file() and (directory / "LeagueToolFlop.dll").is_file():
            return exe
    raise LtkRuntimeError(
        "The LTF bridge launcher and DLL are missing (expected ltk_bridge/ next to "
        "the app, or General.ltfToolsPath): ltk_launcher.exe + LeagueToolFlop.dll")



def _close_ordinary_managers() -> None:
    """Close LTK Manager instances the bridge cannot adopt.

    An instance opened outside the bridge has no DLL injected, so the launcher
    refuses to start ("LTK Manager is already running"), and the user's skin
    selection then does nothing at all. The controlled session is the only one
    this app can drive, so an ordinary instance is asked to quit instead of
    leaving the app dead. A user with unsaved work in the manager sees the
    window close once; the alternative is a silent no-op on every selection.
    """
    victims = [proc for proc in psutil.process_iter(["name"])
               if proc.info.get("name", "").lower() == "ltk-manager.exe"]
    if not victims:
        return
    log.warning("[LTK] Closing %d LTK Manager instance(s) the bridge cannot adopt", len(victims))
    for proc in victims:
        try:
            proc.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(victims, timeout=8)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=5)
    time.sleep(1.0)


def _start_bridge() -> tuple[LtkBridge, bool]:
    launcher, manager = _get_launcher(), _get_manager()
    try:
        result = subprocess.run(
            [str(launcher), "--exe", str(manager), "--quiet"],
            cwd=str(launcher.parent), capture_output=True, text=True,
            timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LtkRuntimeError(f"Could not start LTK Manager through bridge: {exc}") from exc
    combined = f"{result.stdout or ''}\n{result.stderr or ''}"
    if result.returncode != 0:
        raise LtkRuntimeError(
            f"LTF launcher returned {result.returncode}: {combined.strip()[-400:]}"
        )
    match = re.search(r"LTK_MANAGER_PID=(\d+)", result.stdout or "")
    pid = int(match.group(1)) if match else None
    try:
        bridge = LtkBridge.wait_for_bridge(timeout=30, expected_pid=pid)
    except BridgeUnavailable as exc:
        raise LtkRuntimeError(f"LTK bridge did not answer: {exc}") from exc
    return bridge, True


def _connect() -> tuple[LtkBridge, bool]:
    """Attach to a controlled LTK Manager, recovering from the usual blockers.

    Two things made skin selection a silent no-op before this: an ordinary
    instance opened by hand (the bridge refuses to inject into it), and a
    bridge left behind by a previous control session that is no longer
    answering. Both are now cleared and the launch retried once.
    """
    found = find_bridge()
    if found:
        # A bridge from a previous session may still hold the port. Use it if
        # it answers, otherwise close that manager and start a controlled one.
        try:
            return LtkBridge.wait_for_bridge(timeout=5), False
        except BridgeUnavailable:
            log.warning("[LTK] A stale bridge is holding the port; closing its manager")
            _close_ordinary_managers()

    _close_ordinary_managers()
    try:
        return _start_bridge()
    except LtkRuntimeError as exc:
        # One retry: killing an ordinary instance can take a moment, and the
        # launcher reads the process list right after it starts.
        log.warning("[LTK] First bridge start failed (%s); retrying once", exc)
        time.sleep(2.0)
        _close_ordinary_managers()
        return _start_bridge()


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _library_id(library: list[dict], fingerprint: str) -> Optional[str]:
    matches = [str(item["id"]) for item in library
               if isinstance(item, dict) and item.get("id")
               and str(item.get("sourceSha256", "")).lower() == fingerprint.lower()]
    return matches[0] if len(matches) == 1 else None


def _library_id_by_name(library: list[dict], archive: Path) -> Optional[str]:
    stem = archive.stem.lower()
    display = ""
    try:
        with zipfile.ZipFile(archive) as zf:
            for entry in zf.namelist():
                low = entry.lower()
                if low.endswith("meta/info.json"):
                    meta = json.loads(zf.read(entry).decode("utf-8-sig"))
                    display = str(meta.get("Name", "") or "")
                    break
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        display = ""

    wanted = {c for c in (stem, _slugify(display)) if c}
    if not wanted:
        return None

    matches = []
    for item in library:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        names = {str(item.get(k, "")).lower() for k in ("name", "slug")}
        names.add(_slugify(str(item.get("displayName", "") or "")))
        if names & wanted:
            matches.append(str(item["id"]))
    return matches[0] if len(matches) == 1 else None


class LtkRuntime:
    """Own only imports created by this runtime; preserve user library entries."""

    def __init__(self):
        self._lock = threading.RLock()
        self._bridge: Optional[LtkBridge] = None
        self._started_manager = False
        self._imported: set[str] = set()
        self._enabled: set[str] = set()
        self._user_disabled: set[str] = set()
        self._active: Optional[str] = None
        self._scan_ready = False
        self._starting = False

    def _bridge_ready(self) -> LtkBridge:
        if self._bridge and self._bridge.alive():
            return self._bridge
        self._bridge, self._started_manager = _connect()
        deadline = time.monotonic() + 20
        while True:
            try:
                self._bridge.library()
                return self._bridge
            except (BridgeUnavailable, BridgeCommandError) as exc:
                if time.monotonic() >= deadline:
                    raise LtkRuntimeError(f"LTK browser/library is unavailable: {exc}") from exc
                time.sleep(0.5)

    def _activate(self, bridge: LtkBridge, archive: Path) -> str:
        archive = Path(archive)
        if not archive.is_file() or archive.suffix.lower() not in (".fantome", ".modpkg"):
            raise LtkRuntimeError(f"Unsupported skin archive: {archive}")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        before = bridge.library()
        # Managers that publish sourceSha256 can be matched by digest alone.
        mod_id = _library_id(before, digest)
        if mod_id is None:
            # LTK Manager 1.33 rewrites an existing entry instead of adding a
            # second one, so an entry imported by an earlier run is already in
            # ``before``: name/slug is the only stable identity it publishes.
            mod_id = _library_id_by_name(before, archive)
        if mod_id is None:
            old_ids = {str(m.get("id")) for m in before}
            bridge.import_fantome(str(archive), name=archive.stem)
            after = bridge.library()
            mod_id = _library_id(after, digest) or _library_id_by_name(after, archive)
            if mod_id is None:
                # A new import can rewrite its metadata. Identify it only
                # when exactly one new library entry appeared.
                created = [str(m["id"]) for m in after
                           if isinstance(m, dict) and m.get("id")
                           and str(m["id"]) not in old_ids]
                if len(created) == 1:
                    mod_id = created[0]
            if mod_id is None:
                raise LtkRuntimeError("LTK imported archive but its mod ID could not be verified")
            if mod_id not in old_ids:
                self._imported.add(mod_id)
        # A user-enabled mod may conflict with the chosen champion. Preserve
        # the user's switches, and restore them at shutdown.
        for entry in bridge.library():
            other = str(entry.get("id", ""))
            if other and other != mod_id and entry.get("enabled"):
                bridge.toggle(other, False)
                if other not in self._enabled:
                    self._user_disabled.add(other)
                self._enabled.discard(other)
        existing = next((m for m in bridge.library() if str(m.get("id")) == mod_id), {})
        if not existing.get("enabled"):
            bridge.toggle(mod_id, True)
            if mod_id not in self._user_disabled:
                self._enabled.add(mod_id)
        self._active = mod_id
        return mod_id

    def prepare(self, archive: Path) -> None:
        """Make *archive* the one skin the patcher overlays.

        Selecting a skin is the whole user-facing action: whichever archive it
        resolves to must become the enabled mod, every other mod must be
        switched off, and the patcher must be scanning so the next game launch
        gets it. The patcher already running is reused: switching the library
        under it and asking it to rebuild is enough, and a stop/start would
        drop the overlay for a game already in flight. A patcher that is not
        running, or that reports a stale session, is (re)started.
        """
        with self._lock:
            if self._starting:
                return
            self._starting = True
            try:
                bridge = self._bridge_ready()
                mod_id = self._activate(bridge, archive)
                self._ensure_patcher(bridge, mod_id)
            finally:
                self._starting = False

    def _ensure_patcher(self, bridge: LtkBridge, mod_id: str) -> None:
        """Start the patcher unless it is already scanning this library."""
        status = {}
        try:
            status = bridge.patcher_status().get("response", {}).get("value") or {}
        except (BridgeUnavailable, BridgeCommandError) as exc:
            log.warning("[LTK] Could not read the patcher status (%s); restarting it", exc)
        running = bool(status.get("running"))
        phase = str(status.get("phase") or "")
        if running and phase in ("patching", "scanning", "idle"):
            self._scan_ready = True
            log.info("[LTK] Selected mod %s; patcher already %s and reused", mod_id, phase or "running")
            return
        if running:
            # A patched game from a previous session can leave the host busy;
            # a clean restart is the only state it accepts a new overlay in.
            try:
                bridge.stop_patcher()
            except (BridgeUnavailable, BridgeCommandError):
                pass
        bridge.start_patcher()
        self._scan_ready = True
        log.info("[LTK] Selected mod %s; LTK Manager started its patcher", mod_id)

    def running(self) -> bool:
        return bool(self._scan_ready and self._bridge and self._bridge.alive())

    def wait_game(self, stop_callback=None, resume=None) -> bool:
        with self._lock:
            if not self.running():
                raise LtkRuntimeError("LTK patcher is not scanning; cannot resume the game")
        if resume:
            resume()
        # The patcher owns the overlay. Its protocol and WAD redirection are
        # confirmed in LTK Manager's log, not by the old cslol host.
        deadline = time.monotonic() + 7200
        saw_game = False
        while time.monotonic() < deadline:
            if not self.running():
                raise LtkRuntimeError("LTK Manager/bridge stopped before the game ended")
            try:
                game_now = any(proc.info.get("name", "").lower() == "league of legends.exe"
                               for proc in psutil.process_iter(["name"]))
            except psutil.Error:
                game_now = False
            saw_game = saw_game or game_now
            if saw_game and not game_now and (stop_callback is None or stop_callback()):
                break
            time.sleep(0.5)
        return saw_game

    def close(self) -> None:
        with self._lock:
            bridge, self._bridge = self._bridge, None
            if not bridge:
                return
            try:
                if bridge.alive() and self._scan_ready:
                    bridge.stop_patcher()
            except (BridgeUnavailable, BridgeCommandError) as exc:
                log.warning("[LTK] Could not stop patcher: %s", exc)
            for mod_id in list(self._enabled):
                try:
                    if bridge.alive():
                        bridge.toggle(mod_id, False)
                except (BridgeUnavailable, BridgeCommandError) as exc:
                    log.warning("[LTK] Could not restore mod %s: %s", mod_id, exc)
            for mod_id in list(self._imported):
                try:
                    if bridge.alive():
                        bridge.uninstall(mod_id)
                except (BridgeUnavailable, BridgeCommandError) as exc:
                    log.warning("[LTK] Could not remove imported mod %s: %s", mod_id, exc)
            for mod_id in list(self._user_disabled):
                try:
                    if bridge.alive():
                        bridge.toggle(mod_id, True)
                except (BridgeUnavailable, BridgeCommandError) as exc:
                    log.warning("[LTK] Could not restore user mod %s: %s", mod_id, exc)
            if self._started_manager:
                try:
                    bridge.kill()
                except BridgeUnavailable:
                    pass
            self._scan_ready = False
            self._active = None
            self._enabled.clear()
            self._user_disabled.clear()
            self._imported.clear()
            self._started_manager = False
