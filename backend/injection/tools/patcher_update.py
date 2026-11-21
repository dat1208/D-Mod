#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Keep the LTK patcher current with the game.

The DLL refuses a game build newer than the end-of-life date compiled into it,
so a League patch silently turns every injection into a stock-skin game. The
date cannot be changed from outside the binary, and the DLL is not published on
its own: LTK Manager downloads it from a signed delivery service that only its
own builds may talk to. The only supported way to get a newer DLL is therefore
to let LTK Manager fetch it.

This module does that for the user:

  1. If LTK Manager already has a DLL staged on this machine, copy it.
  2. Otherwise install LTK Manager silently, run it once so it downloads the
     DLL, wait for the file to appear, then close it again.
  3. Copy the newest staged DLL (and the host that goes with it) into Rose's
     tools folder, keeping the previous pair as .bak.

Nothing is patched, re-signed or redistributed: the files are the ones LTK
Manager itself downloaded and signed, copied from the user's own machine.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import requests

from config import APP_USER_AGENT
from utils.core.logging import get_logger

from .patcher import (
    LTK_PATCHER_DLL,
    LTK_PATCHER_HOST,
    read_dll_eol,
    read_game_build,
    write_eol_sidecar,
)

log = get_logger()

# Where LTK Manager keeps what it downloaded. Tauri resolves app_data_dir() to
# %APPDATA%\<identifier>, and the patcher lives under "patcher" there, one
# directory per release named after the DLL's SHA-256.
LTK_MANAGER_ID = "dev.leaguetoolkit.manager"
LTK_MANAGER_EXE = "ltk-manager.exe"
LTK_MANAGER_INSTALL_DIR = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "LTK Manager"

# The installer is the only artifact LTK Manager publishes with the host in it;
# the DLL is downloaded by the app afterwards.
RELEASE_API = "https://api.github.com/repos/LeagueToolkit/ltk-manager/releases/latest"
SETUP_NAME = "LTK.Manager_x64-setup.exe"

# How long to wait for LTK Manager to download the DLL on first run. It does so
# right after start, but a slow connection can take a while.
FIRST_RUN_TIMEOUT_S = 180
# How long to wait for the installer to finish.
INSTALL_TIMEOUT_S = 300


class PatcherUpdateError(RuntimeError):
    """The patcher could not be refreshed."""


def _app_data_dir() -> Path:
    """Return LTK Manager's data directory for this user."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise PatcherUpdateError("APPDATA is not set")
    return Path(appdata) / LTK_MANAGER_ID


def staged_dlls() -> list[Path]:
    """Return every DLL LTK Manager has staged, newest first.

    Each release sits in its own directory named after the DLL's SHA-256, so
    the directory name is not ordered; the modification time is.
    """
    root = _app_data_dir() / "patcher"
    if not root.is_dir():
        return []
    found = [p for p in root.glob(f"*/{LTK_PATCHER_DLL}") if p.is_file()]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def _registry_install_locations() -> list:
    """Install locations the LTK Manager uninstall entry reports.

    The NSIS installer honours a directory the machine already used (a fresh
    install here landed in "LTK Manager Probe2", not "LTK Manager"), so the
    registry, not a hardcoded path, is the authority. A missing key or an
    unreadable hive is not an error: the caller falls back to the defaults.
    """
    dirs = []
    if sys.platform != "win32":
        return dirs
    try:
        import winreg
    except ImportError:
        return dirs
    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, path in roots:
        try:
            root = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with root:
            try:
                count = winreg.QueryInfoKey(root)[0]
            except OSError:
                continue
            for index in range(count):
                try:
                    entry = winreg.OpenKey(root, winreg.EnumKey(root, index))
                except OSError:
                    continue
                with entry:
                    try:
                        display = str(winreg.QueryValueEx(entry, "DisplayName")[0])
                    except OSError:
                        continue
                    if "ltk manager" not in display.lower():
                        continue
                    try:
                        location = str(winreg.QueryValueEx(entry, "InstallLocation")[0])
                    except OSError:
                        continue
                    location = location.strip().strip('"').strip()
                    if location:
                        dirs.append(Path(location))
    return dirs


def _manager_exe() -> Optional[Path]:
    """Return the installed LTK Manager executable, if there is one."""
    candidates = [d / LTK_MANAGER_EXE for d in _registry_install_locations()]
    candidates += [
        LTK_MANAGER_INSTALL_DIR / LTK_MANAGER_EXE,
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "LTK Manager" / LTK_MANAGER_EXE,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _download_installer(dest: Path) -> Path:
    """Download the newest LTK Manager installer into dest."""
    log.info("[PATCHER] Fetching the LTK Manager release information")
    session = requests.Session()
    session.headers.update({"User-Agent": APP_USER_AGENT})
    response = session.get(RELEASE_API, timeout=60)
    response.raise_for_status()
    assets = response.json().get("assets") or []
    url = next((a.get("browser_download_url") for a in assets
                if a.get("name") == SETUP_NAME), None)
    if not url:
        raise PatcherUpdateError(f"the release has no {SETUP_NAME}")

    log.info(f"[PATCHER] Downloading LTK Manager: {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with session.get(url, timeout=60, stream=True) as download:
        download.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in download.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    return dest


def _install_manager(installer: Path) -> Path:
    """Install LTK Manager silently and return its executable.

    The installer refuses a directory the user can write to, so the default
    Program Files location is used.
    """
    log.info("[PATCHER] Installing LTK Manager")
    try:
        result = subprocess.run(
            [str(installer), "/S"],
            timeout=INSTALL_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise PatcherUpdateError(f"the installer could not run: {e}") from e
    if result.returncode != 0:
        raise PatcherUpdateError(f"the installer exited with code {result.returncode}")

    # The uninstall key is written at the end of the install; poll briefly so a
    # slow disk or an antivirus scan does not turn a good install into a
    # failure (and into a 23 MB re-download on the next start).
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        exe = _manager_exe()
        if exe is not None:
            return exe
        time.sleep(1.0)
    raise PatcherUpdateError("LTK Manager was installed but its executable was not found")


def _manager_processes() -> list:
    """Every running LTK Manager process, by executable name."""
    if sys.platform != "win32":
        return []
    import psutil

    found = []
    for proc in psutil.process_iter(["name"]):
        try:
            if proc.info.get("name", "").lower() == LTK_MANAGER_EXE:
                found.append(proc)
        except psutil.Error:
            continue
    return found


def _close_managers(keep_alive_s: float = 0.0) -> None:
    """Terminate every LTK Manager process, allowing it to finish its work."""
    if keep_alive_s:
        time.sleep(keep_alive_s)
    victims = _manager_processes()
    if not victims:
        return
    log.info("[PATCHER] Closing LTK Manager")
    for proc in victims:
        try:
            proc.terminate()
        except Exception:
            pass
    for proc in victims:
        try:
            proc.wait(timeout=15)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _run_manager_until_dll(exe: Path, timeout: float) -> Optional[Path]:
    """Run LTK Manager until it stages a DLL, then close it.

    LTK Manager 1.33 relaunches itself as a child of the Windows shell and the
    process this function starts exits about half a second later (its log says
    "Restarted the manager as a child of the shell"). Waiting on that handle
    therefore reports a failure while the real instance is still downloading,
    so the poll looks at the staging directory and at any ltk-manager.exe that
    is still running, not at the handle.

    Returns the staged DLL, or None if none appeared in time.
    """
    log.info("[PATCHER] Starting LTK Manager so it downloads the patcher DLL")
    _close_managers()
    try:
        proc = subprocess.Popen(
            [str(exe)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        raise PatcherUpdateError(f"LTK Manager could not start: {e}") from e

    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            staged = staged_dlls()
            if staged:
                return staged[0]
            # The started handle dying is expected (shell relaunch); only give
            # up once no LTK Manager process is left at all, and even then look
            # one final time in case it staged the DLL just before exiting.
            alive = proc.poll() is None or bool(_manager_processes())
            if not alive:
                staged = staged_dlls()
                return staged[0] if staged else None
            time.sleep(1.0)
        return None
    finally:
        _close_managers(keep_alive_s=0.0)


def _require_valid_signature(path: Path) -> None:
    """Require a valid Windows Authenticode signature for an upstream DLL."""
    if sys.platform != "win32":
        return
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "$s=Get-AuthenticodeSignature -LiteralPath $env:DCHANGER_SIGNED_FILE; "
         "Write-Output $s.Status"],
        env={**os.environ, "DCHANGER_SIGNED_FILE": str(path)},
        capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
    )
    if result.returncode != 0 or result.stdout.strip() != "Valid":
        raise PatcherUpdateError(
            f"{path.name} has no valid Authenticode signature "
            f"({result.stdout.strip() or result.stderr.strip() or 'no status'})"
        )


def _verify_staged_dll(dll: Path) -> None:
    """Reject staged bytes not attested by the delivery manifest and Windows."""
    root = dll.parent.parent
    if dll.name != LTK_PATCHER_DLL or dll.parent.parent.name != "patcher":
        raise PatcherUpdateError("DLL is not in the LTK Manager staging layout")
    if not (root / "delivery.json").is_file():
        raise PatcherUpdateError("LTK Manager delivery manifest is missing")
    digest = hashlib.sha256(dll.read_bytes()).hexdigest()
    if dll.parent.name.lower() != digest:
        raise PatcherUpdateError("staged DLL does not match its SHA-256 directory")
    if read_dll_eol(dll) is None:
        raise PatcherUpdateError("staged DLL has no matching EOL manifest entry")
    _require_valid_signature(dll)


def _backup(target: Path) -> None:
    """Keep the file being replaced as .bak."""
    if not target.is_file():
        return
    backup = target.with_suffix(target.suffix + ".bak")
    try:
        shutil.copy2(target, backup)
    except OSError as e:
        log.warning(f"[PATCHER] Could not back up {target.name}: {e}")


def _host_candidates(dll: Path) -> list:
    """Places a matching ltk_patcher_host.exe can come from, best first.

    LTK Manager 1.33 stages only the DLL under the SHA-256 directory, so the
    host beside it is usually absent. The host ships inside the manager's own
    install directory (same build that delivered the DLL) and is also written
    next to a manager installed by an older release, so both are tried.
    """
    found = [dll.parent / LTK_PATCHER_HOST]
    exe = _manager_exe()
    if exe is not None:
        found.append(exe.parent / LTK_PATCHER_HOST)
    found.append(LTK_MANAGER_INSTALL_DIR / LTK_PATCHER_HOST)
    return found


def _install_pair(dll: Path, tools_dir: Path) -> None:
    """Copy a staged DLL and a matching host into tools_dir."""
    sources = [(LTK_PATCHER_DLL, [dll])]
    for candidate in _host_candidates(dll):
        if candidate.is_file():
            sources.append((LTK_PATCHER_HOST, [candidate]))
            break
    else:
        log.warning(
            "[PATCHER] No ltk_patcher_host.exe was found beside the staged DLL "
            "or in the LTK Manager install; the previous host is kept"
        )

    for name, candidates in sources:
        source = candidates[0]
        target = tools_dir / name
        _backup(target)
        shutil.copy2(source, target)
        log.info(f"[PATCHER] Installed {name} from {source.parent}")

    # The staging manifest stays behind, so record the date beside the copy:
    # without it the copied DLL's end of life cannot be read back.
    write_eol_sidecar(tools_dir / LTK_PATCHER_DLL, read_dll_eol(dll))


def refresh_patcher(tools_dir: Path, game_dir: Optional[Path] = None) -> bool:
    """Fetch a newer LTK patcher when the installed DLL has expired.

    Returns True when new files were installed, False when the installed pair
    is already good enough or could not be improved. Never raises: a failed
    update must not stop the injection, which then reports the DLL's own
    refusal.

    Args:
        tools_dir: Where ltk_patcher_host.exe and ltk_patcher_dll.dll live.
        game_dir: The installed game, used to tell whether a downloaded DLL
            actually covers its build. When None, any newer DLL is accepted.
    """
    tools_dir = Path(tools_dir)
    dll = tools_dir / LTK_PATCHER_DLL

    installed_eol = read_dll_eol(dll) if dll.is_file() else None
    game_build = read_game_build(game_dir) if game_dir else None

    # Do not replace an already adequate DLL; an unknown build still warrants
    # checking when the installed date cannot be determined.
    if installed_eol is not None and game_build is not None and game_build <= installed_eol:
        log.debug("[PATCHER] The installed LTK patcher covers this game build")
        return False

    try:
        staged = staged_dlls()
        if not staged:
            exe = _manager_exe()
            if exe is None:
                with tempfile.TemporaryDirectory(prefix="dchanger-ltk-") as tmp:
                    installer = _download_installer(Path(tmp) / SETUP_NAME)
                    exe = _install_manager(installer)
            staged_dll = _run_manager_until_dll(exe, FIRST_RUN_TIMEOUT_S)
            if staged_dll is None:
                raise PatcherUpdateError("LTK Manager did not download a patcher DLL")
        else:
            staged_dll = staged[0]

        _verify_staged_dll(staged_dll)
        new_eol = read_dll_eol(staged_dll)
        if game_build is not None and new_eol < game_build:
            raise PatcherUpdateError(
                "the newest LTK patcher is still older than this game build "
                f"({time.strftime('%Y-%m-%d %H:%M', time.localtime(new_eol))})"
            )

        _install_pair(staged_dll, tools_dir)
    except Exception as e:
        log.warning(f"[PATCHER] Could not update the LTK patcher: {e}")
        return False

    if new_eol is not None:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(new_eol))
        log.info(f"[PATCHER] Installed a newer LTK patcher (end of life {when})")
    else:
        log.info("[PATCHER] Installed a newer LTK patcher")
    return True
