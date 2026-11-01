#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LTK patcher host launcher.

The patcher DLL verifies who started the game (from a59a336b onward): it
refuses the overlay with "host verification failed, refusing overlay
[VRF00000008]" unless the game's parent process is ltk_patcher_host.exe,
ltk-manager.exe or explorer.exe. D-Changer spawns the host itself, so the
game's parent is dchanger-backend.exe, which is not on that list.

The host is therefore started through explorer.exe, which becomes its parent
and is itself on the allowlist. explorer.exe cannot hand a child our pipes, so
the line protocol is carried by two files in a private directory:

    stdin  - a file the host reads as its standard input. It is opened
             append-only by the host, so commands written later (config, start,
             stop) are read as the host reaches them, exactly like a pipe.
    stdout - a file the host writes its events to, tailed line by line by the
             reader thread.

The host's own stderr goes to a third file, which is appended to the stdout
stream when the host exits so nothing it logged is lost.

Files rather than named pipes: a pipe would need a handle the host inherits,
and explorer.exe (a long-lived process) has no way to pass one to a child it
starts. Files need no handle inheritance, survive the host being killed and
leave the full session in the private directory, which is removed when the
session ends.

Everything here is Windows only. On other platforms, or when explorer.exe
cannot start the host, the caller falls back to spawning the host directly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from utils.core.logging import get_logger

log = get_logger()

# How long explorer.exe gets to start the host before the launch counts as
# failed. It is a shell: normally it returns in milliseconds.
LAUNCH_TIMEOUT_S = 5.0
# How long to wait for the host to create its output file, and how often to
# look for it.
OUTPUT_FILE_TIMEOUT_S = 5.0
OUTPUT_FILE_POLL_S = 0.02
# How long to wait for the host to exit after "stop" before killing it.
STOP_TIMEOUT_S = 5.0
# How long to wait for the host to flush its output file after it exited.
FLUSH_TIMEOUT_S = 0.5
# How often the reader thread checks a quiet output file for new lines.
READER_POLL_S = 0.05

# explorer.exe is a shell: it only starts a child through ShellExecute, and
# ShellExecute does not accept a working directory, so the host is started
# through cmd.exe, which can set one and is itself a child of explorer.exe.
# The host's parent is still explorer.exe, which is what the DLL checks.
CMD_EXE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
EXPLORER_EXE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "explorer.exe")

# Command line for cmd.exe: run the host with its own directory as the working
# directory, redirecting its standard streams to the session files. The host
# path is quoted; it comes from the verified tools directory.
_HOST_COMMAND = '{program} <{stdin} >{stdout} 2>{stderr}'


def _cmd_quote(path: Path) -> str:
    """Quote a path for cmd.exe, which cannot escape a quote inside a path."""
    text = str(path)
    if '"' in text:
        raise ValueError(f"path contains a quote: {text}")
    return f'"{text}"'


@dataclass
class _SessionFiles:
    directory: Path
    stdin: Path
    stdout: Path
    stderr: Path


class PatcherHostProcess:
    """The LTK patcher host started through explorer.exe.

    Presents the part of the subprocess.Popen interface the overlay manager
    uses (poll, wait, returncode, kill, stdin.write/flush/close, stdout,
    stopped_by_rose), so the session handling does not care how the host was
    started.
    """

    def __init__(self, files: _SessionFiles, launcher: subprocess.Popen, host_exe: Path):
        self.files = files
        self.launcher = launcher
        self.host_exe = host_exe
        self.stopped_by_rose = False
        self.returncode: Optional[int] = None
        self._lock = threading.Lock()
        self._stdin = open(files.stdin, "a", encoding="utf-8", errors="replace", newline="")
        # Read-write: the reader thread reads it and drain_stderr appends to it
        self._stdout = open(files.stdout, "r+", encoding="utf-8", errors="replace", newline="")
        self._stderr = open(files.stderr, "r", encoding="utf-8", errors="replace", newline="")
        self._output_offset = 0
        # Set once stderr was appended to the output: until then the reader
        # must not treat the host as gone, or it would miss those last lines
        self._stderr_drained = False

    # -- stdin ------------------------------------------------------------

    @property
    def stdin(self):
        return self

    def write(self, text: str) -> int:
        with self._lock:
            self._stdin.write(text)
        return len(text)

    def flush(self) -> None:
        with self._lock:
            self._stdin.flush()

    def close(self) -> None:
        """Close our end of the command file: the host reads it to its end."""
        with self._lock:
            if not self._stdin.closed:
                self._stdin.close()

    # -- stdout -----------------------------------------------------------

    @property
    def stdout(self):
        return self

    def readline(self) -> str:
        """The next complete line the host wrote, or "" once it exited.

        The output file is read from the last position, so a partial line is
        left for the next call instead of being returned cut in half.
        """
        while True:
            line = self._next_line()
            if line is not None:
                return line
            if self._host_exited():
                # The host is gone and its stderr is in: all there is is here
                line = self._next_line()
                return line if line is not None else ""
            time.sleep(READER_POLL_S)

    def __iter__(self):
        while True:
            line = self.readline()
            if not line:
                return
            yield line

    def _next_line(self) -> Optional[str]:
        with self._lock:
            self._stdout.seek(self._output_offset)
            line = self._stdout.readline()
            if line and line.endswith("\n"):
                self._output_offset = self._stdout.tell()
                return line
            # No complete line yet (or none at all): read it again next time.
            # A last line the host wrote without a newline is returned once
            # the host is gone, so nothing it wrote is lost.
            if line and self._host_exited():
                self._output_offset = self._stdout.tell()
                return line
            return None

    # -- lifecycle --------------------------------------------------------

    def _host_gone(self) -> bool:
        """True once the host process itself is gone.

        explorer.exe returns as soon as it started the host, so it says
        nothing about the host itself: the host is looked up by its image
        path instead. Without psutil there is no way to tell, and the host is
        assumed to still run so its output is never cut short.
        """
        if self.launcher.poll() is None:
            return False
        try:
            import psutil
        except ImportError:
            return False
        try:
            for proc in psutil.process_iter(["name", "exe"]):
                try:
                    if (proc.info.get("name") or "").lower() != self.host_exe.name.lower():
                        continue
                    exe = proc.info.get("exe")
                    if exe and Path(exe).resolve() == self.host_exe.resolve():
                        return False
                except psutil.Error:
                    continue
        except psutil.Error:
            return False
        return True

    def _host_exited(self) -> bool:
        """True once the host is gone and its stderr reached the reader.

        The reader stops on this, so the host's last lines (the DLL's refusal
        on stderr, for one) are never cut off.
        """
        return self._host_gone() and self._stderr_drained

    def poll(self) -> Optional[int]:
        if self.returncode is not None:
            return self.returncode
        if not self._host_gone():
            return None
        self.returncode = self._exit_code()
        return self.returncode

    def wait(self, timeout: Optional[float] = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(str(self.host_exe), timeout)
            time.sleep(READER_POLL_S)
        return self.returncode

    def kill(self) -> None:
        """Kill the host itself: explorer.exe only started it and is long gone."""
        try:
            import psutil
        except ImportError:
            log.warning("[INJECT] psutil is unavailable - cannot kill the LTK patcher host")
            return
        try:
            for proc in psutil.process_iter(["name", "exe"]):
                try:
                    if (proc.info.get("name") or "").lower() != self.host_exe.name.lower():
                        continue
                    exe = proc.info.get("exe")
                    if exe and Path(exe).resolve() != self.host_exe.resolve():
                        continue
                    proc.kill()
                except psutil.Error:
                    continue
        except psutil.Error as e:
            log.warning(f"[INJECT] Could not kill the LTK patcher host: {e}")

    def _exit_code(self) -> int:
        """The host's exit code, from the file it wrote it to (cmd.exe cannot
        report a child's code), or the launcher's code if it never got that far."""
        try:
            code = int(self.files.directory.joinpath("exit_code").read_text().strip())
            return code
        except (OSError, ValueError):
            return self.launcher.returncode or 0

    # -- output -----------------------------------------------------------

    def drain_stderr(self) -> None:
        """Append what the host wrote to stderr to its output stream."""
        with self._lock:
            self._stderr.seek(0)
            text = self._stderr.read()
            if text:
                self._stdout.seek(0, os.SEEK_END)
                if not text.endswith("\n"):
                    text += "\n"
                self._stdout.write(text)
                self._stdout.flush()
            # From here the reader may see the host as gone; it reads the
            # appended lines from its own position, so the offset is left alone
            self._stderr_drained = True

    def cleanup(self) -> None:
        for stream in (self._stdin, self._stdout, self._stderr):
            try:
                stream.close()
            except Exception:
                pass
        shutil.rmtree(self.files.directory, ignore_errors=True)


def _wait_for_file(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(OUTPUT_FILE_POLL_S)
    return path.exists()


def _write_exit_code(files: _SessionFiles, code: int) -> None:
    try:
        files.directory.joinpath("exit_code").write_text(str(code), encoding="ascii")
    except OSError:
        pass


def _launch_through_explorer(host_exe: Path, files: _SessionFiles,
                             command: Optional[list] = None) -> subprocess.Popen:
    """Start the host from explorer.exe and return the launcher process.

    Raises when explorer.exe could not be started or the host never created
    its output file, so the caller can fall back to a direct spawn.
    """
    program = " ".join(_cmd_quote(Path(part)) for part in command) if command else _cmd_quote(host_exe)
    line = (
        f'"{CMD_EXE}" /d /s /c "'
        + _HOST_COMMAND.format(
            program=program,
            stdin=_cmd_quote(files.stdin),
            stdout=_cmd_quote(files.stdout),
            stderr=_cmd_quote(files.stderr),
        )
        + f' & echo %ERRORLEVEL% > "{files.directory / "exit_code"}"'
        + '"'
    )
    log.debug(f"[INJECT] Starting LTK patcher through explorer.exe: {line}")
    launcher = subprocess.Popen(
        [EXPLORER_EXE, line],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        launcher.wait(timeout=LAUNCH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        launcher.kill()
        launcher.wait()
        raise RuntimeError("explorer.exe did not return after starting the LTK patcher")
    if not _wait_for_file(files.stdout, OUTPUT_FILE_TIMEOUT_S):
        raise RuntimeError("explorer.exe did not start the LTK patcher (no output)")
    return launcher


def launch_ltk_patcher_host(host_exe: Path, command: Optional[list] = None) -> Optional[PatcherHostProcess]:
    """Start the LTK patcher host with explorer.exe as its parent.

    command overrides what cmd.exe runs (the host executable by default); the
    tests use it to run a stand-in for the host.

    Returns None when the host could not be started this way, so the caller
    can spawn it directly instead.
    """
    if sys.platform != "win32":
        return None
    try:
        directory = Path(tempfile.mkdtemp(prefix="dchanger-ltk-"))
    except OSError as e:
        log.warning(f"[INJECT] Could not create the LTK patcher session directory: {e}")
        return None
    files = _SessionFiles(
        directory=directory,
        stdin=directory / "stdin.txt",
        stdout=directory / "stdout.txt",
        stderr=directory / "stderr.txt",
    )
    try:
        files.stdin.touch()
        files.stdout.touch()
        files.stderr.touch()
        launcher = _launch_through_explorer(host_exe, files, command)
    except Exception as e:
        log.warning(f"[INJECT] Could not start the LTK patcher through explorer.exe: {e}")
        shutil.rmtree(directory, ignore_errors=True)
        return None
    log.info("[INJECT] LTK patcher started through explorer.exe (parent process check)")
    return PatcherHostProcess(files, launcher, host_exe)


def spawn_ltk_patcher_host(host_exe: Path) -> subprocess.Popen:
    """Start the host directly, as a child of D-Changer.

    Only used when the explorer.exe launch failed: the DLL refuses the overlay
    when it verifies the parent, but the host still reports why.
    """
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    return subprocess.Popen(
        [str(host_exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=str(host_exe.parent), creationflags=creationflags,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
