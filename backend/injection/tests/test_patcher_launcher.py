import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from injection.overlay import overlay_manager, patcher_launcher
from injection.overlay.overlay_manager import OverlayManager
from injection.overlay.patcher_launcher import PatcherHostProcess, launch_ltk_patcher_host


class FakeHost(PatcherHostProcess):
    """The host started through explorer.exe, with the host itself simulated.

    The real host cannot be started here (explorer.exe does not run a command
    line outside a desktop shell), so this stands in for it: it reads the
    commands written to the session's stdin file and writes the line protocol
    events the real host writes to the session's stdout file.
    """

    def __init__(self, files, launcher, host_exe):
        super().__init__(files, launcher, host_exe)
        self.received = []
        self._alive = True
        # The host writes the session files itself; PatcherHostProcess only
        # holds read handles on them.
        self._out = open(files.stdout, 'a', encoding='utf-8', newline='')
        self._err = open(files.stderr, 'a', encoding='utf-8', newline='')
        self._host = threading.Thread(target=self._run, daemon=True)
        self._host.start()

    def _run(self):
        while self._alive:
            # The host reads its command file itself: the session closes its
            # own handle on stop, which must not affect the host's view.
            try:
                lines = self.files.stdin.read_text(encoding='utf-8').splitlines()
            except OSError:
                lines = []
            for line in lines[len(self.received):]:
                self.received.append(line)
                if line == 'start scan':
                    self._out.write('status 1 scanning\n')
                    self._out.flush()
                elif line == 'stop':
                    self._out.write('status 2 stopped\n')
                    self._out.flush()
                    self._err.write('dll 1 ERROR host verification failed, refusing overlay\n')
                    self._err.flush()
                    # cmd.exe cannot report the child's code, so the host writes it
                    (self.files.directory / 'exit_code').write_text('0', encoding='ascii')
                    # Only now is the host gone, so the session's drain_stderr
                    # (which runs after _stop_ltk_patcher returns) still lands
                    self._alive = False
            threading.Event().wait(0.01)

    def _host_gone(self):
        return not self._alive

    def cleanup(self):
        self._alive = False
        self._host.join(timeout=2)
        self._out.close()
        self._err.close()
        super().cleanup()


class LauncherTests(unittest.TestCase):
    """The DLL only overlays games whose parent is on its allowlist, so the
    host is started from explorer.exe"""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name)
        self.host = self.root / 'tools' / 'ltk_patcher_host.exe'
        self.host.parent.mkdir()
        self.host.write_bytes(b'host')
        self.overlay_dir = self.root / 'overlay'
        self.overlay_dir.mkdir()
        self.overlay = OverlayManager(self.root / 'tools', self.root / 'mods', self.root / 'game')

        reporter = patch.object(overlay_manager, 'report_issue')
        self.report_issue = reporter.start()
        self.addCleanup(reporter.stop)

    def _start(self, launch=None):
        launch = launch or (lambda host: self._fake_host(host))
        with patch.object(patcher_launcher, 'launch_ltk_patcher_host', side_effect=launch):
            session = self.overlay._start_ltk_patcher(self.host, self.overlay_dir)
        if session is not None:
            self.addCleanup(session['proc'].cleanup)
        return session

    def _fake_host(self, host_exe):
        """A PatcherHostProcess whose host is simulated in-process."""
        directory = Path(tempfile.mkdtemp(prefix='dchanger-ltk-test-'))
        files = patcher_launcher._SessionFiles(
            directory=directory,
            stdin=directory / 'stdin.txt',
            stdout=directory / 'stdout.txt',
            stderr=directory / 'stderr.txt',
        )
        for path in (files.stdin, files.stdout, files.stderr):
            path.touch()
        launcher = MagicMock()
        launcher.poll.return_value = 0  # explorer.exe returned after starting the host
        return FakeHost(files, launcher, host_exe)

    def test_the_host_is_started_through_explorer(self):
        session = self._start()

        self.assertIsInstance(session['proc'], PatcherHostProcess)
        self.assertIsInstance(session['proc'], FakeHost)

    def test_the_command_goes_through_explorer_in_a_script(self):
        files = patcher_launcher._SessionFiles(
            directory=self.root, stdin=self.root / 'in', stdout=self.root / 'out', stderr=self.root / 'err',
        )
        launcher = MagicMock()
        launcher.wait.return_value = 0
        with patch.object(patcher_launcher.subprocess, 'Popen', return_value=launcher) as popen, \
                patch.object(patcher_launcher, '_wait_for_file', return_value=True):
            patcher_launcher._launch_through_explorer(self.host, files)

        # explorer.exe only opens a file, so the command lives in a script
        argv = popen.call_args[0][0]
        self.assertEqual(argv[0], patcher_launcher.EXPLORER_EXE)
        script = Path(argv[1])
        self.assertEqual(script, self.root / 'run.bat')
        text = script.read_text(encoding='mbcs' if os.name == 'nt' else 'utf-8')
        self.assertIn(f'"{self.host}"', text)
        self.assertIn(f'<"{files.stdin}"', text)
        self.assertIn(f'>"{files.stdout}"', text)
        self.assertIn(f'2>"{files.stderr}"', text)
        self.assertIn('exit_code', text)
        self.assertEqual(popen.call_args[1]['creationflags'], subprocess.CREATE_NO_WINDOW)

    def test_stdout_lines_reach_the_parser(self):
        session = self._start()
        session['reader'].join(timeout=10)

        self.assertEqual(session['session']['state'], 'scanning')
        self.assertEqual(session['proc'].received[:3], [
            f'config loglevel {overlay_manager.LTK_PATCHER_LOG_LEVEL}',
            f'config flags {overlay_manager.LTK_PATCHER_FLAGS}',
            f'config prefix {str(self.overlay_dir).rstrip(chr(92) + "/")}\\',
        ])

    def test_stopping_the_host_keeps_its_last_lines(self):
        session = self._start()
        proc = session['proc']

        self.assertIsNone(proc.poll())
        OverlayManager._stop_ltk_patcher(proc)
        session['reader'].join(timeout=10)

        self.assertEqual(proc.poll(), 0)
        self.assertIn('host verification failed', proc.files.stdout.read_text(encoding='utf-8'))
        # The DLL's refusal on stderr is parsed like any other event
        self.assertIn('host verification failed', session['session']['error'] or '')

    def test_a_failed_indirection_falls_back_to_a_direct_spawn(self):
        direct = MagicMock()
        with patch.object(patcher_launcher, 'launch_ltk_patcher_host', return_value=None), \
                patch.object(patcher_launcher, 'spawn_ltk_patcher_host', return_value=direct) as spawn:
            session = self.overlay._start_ltk_patcher(self.host, self.overlay_dir)

        spawn.assert_called_once_with(self.host)
        self.assertIs(session['proc'], direct)
        direct.stdin.write.assert_any_call('start scan\n')

    def test_a_launch_that_raises_falls_back_to_a_direct_spawn(self):
        direct = MagicMock()
        with patch.object(patcher_launcher, 'launch_ltk_patcher_host', side_effect=OSError('no explorer')), \
                patch.object(patcher_launcher, 'spawn_ltk_patcher_host', return_value=direct) as spawn:
            session = self.overlay._start_ltk_patcher(self.host, self.overlay_dir)

        spawn.assert_called_once_with(self.host)
        self.assertIs(session['proc'], direct)

    def test_a_direct_spawn_that_fails_is_reported(self):
        with patch.object(patcher_launcher, 'launch_ltk_patcher_host', return_value=None), \
                patch.object(patcher_launcher, 'spawn_ltk_patcher_host', side_effect=OSError('missing')):
            session = self.overlay._start_ltk_patcher(self.host, self.overlay_dir)

        self.assertIsNone(session)
        self.assertIn('could not start', self.report_issue.call_args[0][2])

    def test_a_failed_indirection_leaves_no_session_directory(self):
        before = set(Path(tempfile.gettempdir()).glob('dchanger-ltk-*'))
        launcher = MagicMock()
        launcher.wait.return_value = 0
        with patch.object(patcher_launcher.subprocess, 'Popen', return_value=launcher), \
                patch.object(patcher_launcher, '_wait_for_file', return_value=False):
            self.assertIsNone(launch_ltk_patcher_host(self.host))

        self.assertEqual(set(Path(tempfile.gettempdir()).glob('dchanger-ltk-*')), before)

    def test_an_explorer_that_never_returns_is_killed_and_falls_back(self):
        launcher = MagicMock()
        launcher.wait.side_effect = subprocess.TimeoutExpired('explorer.exe', 5)
        with patch.object(patcher_launcher.subprocess, 'Popen', return_value=launcher), \
                patch.object(patcher_launcher, 'LAUNCH_TIMEOUT_S', 0.01):
            self.assertIsNone(launch_ltk_patcher_host(self.host))

        launcher.kill.assert_called_once()

    def test_other_platforms_start_the_host_directly(self):
        with patch.object(patcher_launcher.sys, 'platform', 'linux'):
            self.assertIsNone(launch_ltk_patcher_host(self.host))

    def test_the_session_directory_is_removed_with_the_session(self):
        session = self._start()
        directory = session['proc'].files.directory
        self.assertTrue(directory.is_dir())

        self.overlay._abort_ltk_patcher(session)
        self.assertFalse(directory.exists())


class HostProcessTests(unittest.TestCase):
    """The host process presents the Popen interface the session code uses"""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.directory = Path(temp_dir.name)
        self.files = patcher_launcher._SessionFiles(
            directory=self.directory,
            stdin=self.directory / 'stdin.txt',
            stdout=self.directory / 'stdout.txt',
            stderr=self.directory / 'stderr.txt',
        )
        for path in (self.files.stdin, self.files.stdout, self.files.stderr):
            path.touch()
        self.launcher = MagicMock()
        self.launcher.poll.return_value = 0  # explorer.exe already returned
        self.proc = PatcherHostProcess(self.files, self.launcher, Path('ltk_patcher_host.exe'))
        self.addCleanup(self.proc.cleanup)

    def test_a_partial_line_is_not_returned_cut_in_half(self):
        """The reader must not see half a line while the host is still writing"""
        self.files.stdout.write_text('status 1 sca', encoding='utf-8', newline='\n')
        self.assertIsNone(self.proc._next_line())
        self.files.stdout.write_text('status 1 scanning\n', encoding='utf-8', newline='\n')
        self.assertEqual(self.proc._next_line(), 'status 1 scanning\n')

    def test_the_last_partial_line_is_returned_once_the_host_exited(self):
        """A host killed mid-line still gets its last line parsed"""
        self.files.stdout.write_text('status 1 scanning', encoding='utf-8', newline='\n')
        with patch.object(self.proc, '_host_exited', return_value=True):
            self.assertEqual(self.proc.readline(), 'status 1 scanning')

    def test_commands_are_appended_to_the_host_input(self):
        self.proc.stdin.write('config flags 4\n')
        self.proc.stdin.flush()
        self.proc.stdin.write('start scan\n')
        self.proc.stdin.close()
        self.assertEqual(self.files.stdin.read_text(encoding='utf-8'), 'config flags 4\nstart scan\n')

    def test_the_exit_code_is_read_from_the_session(self):
        (self.directory / 'exit_code').write_text('3', encoding='ascii')
        # The test holds the output file open itself, so the host's own
        # "still holds it" probe cannot be used here
        with patch.object(self.proc, '_host_exited', return_value=True):
            self.assertEqual(self.proc.poll(), 3)
        self.assertEqual(self.proc.returncode, 3)

    def test_a_host_that_still_holds_its_output_is_running(self):
        self.launcher.poll.return_value = None
        self.assertIsNone(self.proc.poll())


if __name__ == '__main__':
    unittest.main()
