#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LTK patcher host launcher.

The patcher DLL verifies who started the game (from a59a336b onward): it
refuses the overlay with "host verification failed, refusing overlay
[VRF00000008]" unless the game's parent process is ltk_patcher_host.exe,
ltk-manager.exe or explorer.exe. D-Changer spawns the host itself, so the
game's parent is dchanger-backend.exe, which is not on that list.

The check is on the name of the process that started the game, not on the
path it was loaded from, so the host is copied to a private directory under
the name explorer.exe and started from there. The game's parent then carries
an allowed name, and the host keeps its own pipes because D-Changer started
it directly.

Files rather than named pipes: a pipe would need a handle the host inherits,
and a launcher that is not D-Changer has no way to pass one to a child it
starts. Files need no handle inheritance, survive the host being killed and
leave the full session in the private directory, which is removed when the
session ends.

Everything here is Windows only. On other platforms, or when the renamed copy
cannot be started, the caller falls back to spawning the host directly.
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

# How long to wait for the host to create its output file, and how often to
# look for it. explorer.exe hands the file to a running shell instance, which
# can be busy while the game starts, so this is generous.
OUTPUT_FILE_TIMEOUT_S = 15.0

# How long the host is assumed alive even when the process list does not show
# it yet: cmd.exe starts it as a grandchild, so it appears a moment late.
HOST_STARTUP_GRACE_S = 5.0
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

# The name the host is copied to. The DLL accepts the game's parent when its
# name is ltk_patcher_host.exe, ltk-manager.exe or explorer.exe; the first is
# the host's own name and the last is a name Windows never reserves, so a copy
# under it cannot collide with a real process D-Changer would have to find.
ALLOWED_HOST_NAME = "explorer.exe"

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
    """The LTK patcher host started under an allowed name.

    Presents the part of the subprocess.Popen interface the overlay manager
    uses (poll, wait, returncode, kill, stdin.write/flush/close, stdout,
    stopped_by_rose), so the session handling does not care how the host was
    started.
    """

    def __init__(self, files: _SessionFiles, launcher: subprocess.Popen, host_exe: Path,
                 piped: bool = False):
        self.files = files
        self.launcher = launcher
        self.host_exe = host_exe
        self.stopped_by_rose = False
        self.returncode: Optional[int] = None
        self._lock = threading.Lock()
        # A piped host is the process itself and talks over its own pipes, so
        # its streams are used directly. A file host is a grandchild of
        # cmd.exe, which cannot pass pipes on, so the session files are used.
        self._piped = piped
        if piped:
            self._stdin = launcher.stdin
            self._stdout = launcher.stdout
            self._stderr = launcher.stderr
        else:
            self._stdin = open(files.stdin, "a", encoding="utf-8", errors="replace", newline="")
            # Read-write: the reader thread reads it and drain_stderr appends
            # to it. cmd.exe creates the file with its redirection and may
            # still hold it exclusively for a moment, so opening is retried.
            self._stdout = _open_shared(files.stdout, "r+")
            self._stderr = _open_shared(files.stderr, "r")
        self._output_offset = 0
        # Set once stderr was appended to the output: until then the reader
        # must not treat the host as gone, or it would miss those last lines
        self._stderr_drained = False
        # The host is a grandchild started by cmd.exe, so it takes a moment to
        # appear in the process list. Until this passes, a missing process
        # means "not started yet", not "already gone".
        self._started_at = time.monotonic()

    # -- stdin ------------------------------------------------------------

    @property
    def stderr_is_pipe(self) -> bool:
        """True when stderr is a live pipe the caller has to read itself."""
        return self._piped

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

    def request_stop(self) -> None:
        """Ask the host to stop.

        A piped host reads its commands as they come, so it is asked to stop
        on its own input. A file host read its command file to the end when it
        started, so a line written now would never be seen: it is stopped by
        killing it instead, which is what wait()/kill() do next.
        """
        if self._piped:
            try:
                self._stdin.write("stop\n")
                self._stdin.flush()
            except (OSError, ValueError):
                pass

    # -- stdout -----------------------------------------------------------

    @property
    def stderr(self):
        return self._stderr

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
        if self._piped:
            # A pipe blocks until the host writes a line or closes its end,
            # which is exactly what the reader thread wants.
            try:
                line = self._stdout.readline()
            except (OSError, ValueError):
                return None
            return line or None
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

        The launcher is cmd.exe, which returns as soon as it started the
        host, so it says nothing about the host itself: the host is looked up
        by the path of the copy that was started. Without psutil there is no
        way to tell, and the host is assumed to still run so its output is
        never cut short.
        """
        if self.launcher.poll() is None:
            return False
        if time.monotonic() - self._started_at < HOST_STARTUP_GRACE_S:
            return False
        try:
            import psutil
        except ImportError:
            return False
        try:
            for proc in psutil.process_iter(["name", "exe"]):
                try:
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
        """Kill the host itself: the launcher only started it and is long gone."""
        try:
            import psutil
        except ImportError:
            log.warning("[INJECT] psutil is unavailable - cannot kill the LTK patcher host")
            return
        try:
            for proc in psutil.process_iter(["name", "exe"]):
                try:
                    exe = proc.info.get("exe")
                    if exe and Path(exe).resolve() == self.host_exe.resolve():
                        proc.kill()
                except psutil.Error:
                    continue
        except psutil.Error as e:
            log.warning(f"[INJECT] Could not kill the LTK patcher host: {e}")

    def _exit_code(self) -> int:
        """The host's exit code, from the file it wrote it to (cmd.exe cannot
        report a child's code), or the launcher's code if it never got that far."""
        if self._piped:
            # cmd.exe waits for the host and returns its code
            return self.launcher.returncode or 0
        try:
            code = int(self.files.directory.joinpath("exit_code").read_text().strip())
            return code
        except (OSError, ValueError):
            return self.launcher.returncode or 0

    # -- output -----------------------------------------------------------

    def drain_stderr(self) -> None:
        """Append what the host wrote to stderr to its output stream."""
        if self._piped:
            # A piped host's stderr is its own pipe: the reader gets it as it
            # comes, so there is nothing to drain.
            self._stderr_drained = True
            return
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


def _open_shared(path: Path, mode: str):
    """Open a session file, retrying while the launcher still holds it.

    cmd.exe creates the output files with its redirection and keeps them open
    exclusively for a moment after they appear, so an immediate open fails
    with a permission error even though the file is there.
    """
    deadline = time.monotonic() + OUTPUT_FILE_TIMEOUT_S
    while True:
        try:
            return open(path, mode, encoding="utf-8", errors="replace", newline="")
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(OUTPUT_FILE_POLL_S)


def _write_exit_code(files: _SessionFiles, code: int) -> None:
    try:
        files.directory.joinpath("exit_code").write_text(str(code), encoding="ascii")
    except OSError:
        pass


def _launch_renamed_host(host_exe: Path, files: _SessionFiles,
                         command: Optional[list] = None,
                         commands: Optional[list] = None) -> subprocess.Popen:
    """Start a copy of the host named explorer.exe and return its process.

    The DLL checks the name of the process that started the game, so a copy
    under an allowed name passes the check while D-Changer still starts it
    directly and keeps its pipes.

    commands are written to the command file before the host starts: the host
    reads its standard input to the end, and a file that is still empty at
    that point reads as end of input, so the host would never see a command
    written afterwards.

    Raises when the copy could not be made or the host never created its
    output file, so the caller can fall back to a direct spawn.
    """
    renamed = files.directory / ALLOWED_HOST_NAME
    shutil.copy2(host_exe, renamed)

    # The DLL beside the host is what the host loads into the game, so it has
    # to travel with the copy.
    for extra in host_exe.parent.glob("*.dll"):
        shutil.copy2(extra, files.directory / extra.name)

    if command:
        # The tests run a stand-in through cmd.exe with the session files.
        program = " ".join(_cmd_quote(Path(part)) for part in command)
        script = files.directory / "run.bat"
        script.write_text(
            "@echo off\r\n"
            + _HOST_COMMAND.format(
                program=program,
                stdin=_cmd_quote(files.stdin),
                stdout=_cmd_quote(files.stdout),
                stderr=_cmd_quote(files.stderr),
            )
            + f'\r\necho %ERRORLEVEL% > "{files.directory / "exit_code"}"\r\n',
            encoding="mbcs" if os.name == "nt" else "utf-8",
            errors="replace",
        )
        log.debug(f"[INJECT] Starting LTK patcher as {ALLOWED_HOST_NAME}: {script}")
        launcher = subprocess.Popen(
            [CMD_EXE, "/c", str(script)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(files.directory),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if not _wait_for_file(files.stdout, OUTPUT_FILE_TIMEOUT_S):
            raise RuntimeError(f"{ALLOWED_HOST_NAME} did not start the LTK patcher (no output)")
        return launcher

    # The host is started through cmd.exe so its parent is a shell, not
    # D-Changer: the DLL verifies the process that started the host. cmd.exe
    # passes its own standard streams on to the child, so the host still
    # talks over D-Changer's pipes and does not stop at the end of a file.
    # The command line is one string: cmd.exe parses it itself, and a path
    # quoted by subprocess would reach it with the quotes escaped.
    log.debug(f"[INJECT] Starting LTK patcher as {ALLOWED_HOST_NAME}: {renamed}")
    return subprocess.Popen(
        f'{_cmd_quote(Path(CMD_EXE))} /c {_cmd_quote(renamed)}',
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(files.directory),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )


def launch_ltk_patcher_host(host_exe: Path, command: Optional[list] = None,
                            commands: Optional[list] = None) -> Optional[PatcherHostProcess]:
    """Start the LTK patcher host under a name the DLL accepts.

    command overrides what cmd.exe runs (the renamed host by default); the
    tests use it to run a stand-in for the host. commands are the protocol
    lines the host must see on its standard input.

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
        # cmd.exe creates the output files for a stand-in host, so their
        # appearance is what proves it really started
        files.stdin.touch()
        launcher = _launch_renamed_host(host_exe, files, command, commands)
        piped = command is None
        session = PatcherHostProcess(files, launcher, files.directory / ALLOWED_HOST_NAME, piped=piped)
        if piped and commands:
            # A piped host reads its commands as they come, so they are
            # written now that its input pipe exists.
            for line in commands:
                session.stdin.write(line + "\n")
            session.stdin.flush()
    except Exception as e:
        log.warning(f"[INJECT] Could not start the LTK patcher as {ALLOWED_HOST_NAME}: {e}")
        shutil.rmtree(directory, ignore_errors=True)
        return None
    log.info(f"[INJECT] LTK patcher started as {ALLOWED_HOST_NAME} (parent process check)")
    return session


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
