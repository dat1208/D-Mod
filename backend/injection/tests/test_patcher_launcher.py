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

    def request_stop(self):
        """The real host cannot read a line after it started, so stopping it
        is a kill; the stand-in just stops."""
        self._out.write('status 2 stopped\n')
        self._out.flush()
        self._err.write('dll 1 ERROR host verification failed, refusing overlay\n')
        self._err.flush()
        (self.files.directory / 'exit_code').write_text('0', encoding='ascii')
        self._alive = False

    def cleanup(self):
        self._alive = False
        self._host.join(timeout=2)
        self._out.close()
        self._err.close()
        super().cleanup()


class LauncherTests(unittest.TestCase):
    """The DLL only overlays games whose parent is on its allowlist, so the
    host is copied under an allowed name and started from there"""

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
        launch = launch or (lambda host, **kwargs: self._fake_host(host, **kwargs))
        with patch.object(patcher_launcher, 'launch_ltk_patcher_host', side_effect=launch):
            session = self.overlay._start_ltk_patcher(self.host, self.overlay_dir)
        if session is not None:
            self.addCleanup(session['proc'].cleanup)
        return session

    def _fake_host(self, host_exe, commands=None):
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
        # The real host reads its command file before it starts, so the
        # commands are already in it when the stand-in begins.
        if commands:
            files.stdin.write_text(''.join(f'{line}\n' for line in commands), encoding='utf-8')
        launcher = MagicMock()
        launcher.poll.return_value = 0  # the launcher returned after starting the host
        return FakeHost(files, launcher, host_exe)

    def test_the_host_is_started_under_an_allowed_name(self):
        session = self._start()

        self.assertIsInstance(session['proc'], PatcherHostProcess)
        self.assertIsInstance(session['proc'], FakeHost)

    def test_the_host_is_copied_under_an_allowed_name(self):
        files = patcher_launcher._SessionFiles(
            directory=self.root, stdin=self.root / 'in', stdout=self.root / 'out', stderr=self.root / 'err',
        )
        launcher = MagicMock()
        with patch.object(patcher_launcher.subprocess, 'Popen', return_value=launcher), \
                patch.object(patcher_launcher, '_wait_for_file', return_value=True):
            patcher_launcher._launch_renamed_host(self.host, files)

        renamed = self.root / patcher_launcher.ALLOWED_HOST_NAME
        self.assertTrue(renamed.is_file())
        self.assertEqual(renamed.read_bytes(), self.host.read_bytes())

    def test_the_host_is_started_through_a_shell_with_pipes(self):
        """The DLL verifies the host's parent, so it runs under cmd.exe, but
        its streams are pipes so it does not stop at the end of a file."""
        files = patcher_launcher._SessionFiles(
            directory=self.root, stdin=self.root / 'in', stdout=self.root / 'out', stderr=self.root / 'err',
        )
        launcher = MagicMock()
        with patch.object(patcher_launcher.subprocess, 'Popen', return_value=launcher) as popen:
            patcher_launcher._launch_renamed_host(self.host, files)

        argv = popen.call_args[0][0]
        # One command line: cmd.exe parses it, and a list would have the
        # path's quotes escaped by subprocess
        self.assertIsInstance(argv, str)
        self.assertTrue(argv.startswith(f'"{patcher_launcher.CMD_EXE}"'))
        self.assertIn(' /c ', argv)
        self.assertIn(f'"{self.root / patcher_launcher.ALLOWED_HOST_NAME}"', argv)
        self.assertEqual(popen.call_args[1]['stdin'], subprocess.PIPE)
        self.assertEqual(popen.call_args[1]['stdout'], subprocess.PIPE)
        self.assertEqual(popen.call_args[1]['stderr'], subprocess.PIPE)
        self.assertEqual(popen.call_args[1]['creationflags'], subprocess.CREATE_NO_WINDOW)

    def test_a_stand_in_host_runs_through_a_script(self):
        """The tests' stand-in is not the real host, so it still uses the files."""
        files = patcher_launcher._SessionFiles(
            directory=self.root, stdin=self.root / 'in', stdout=self.root / 'out', stderr=self.root / 'err',
        )
        launcher = MagicMock()
        with patch.object(patcher_launcher.subprocess, 'Popen', return_value=launcher) as popen, \
                patch.object(patcher_launcher, '_wait_for_file', return_value=True):
            patcher_launcher._launch_renamed_host(self.host, files, command=['stand-in.exe'])

        argv = popen.call_args[0][0]
        self.assertEqual(argv[0], patcher_launcher.CMD_EXE)
        script = Path(argv[2])
        self.assertEqual(script, self.root / 'run.bat')
        text = script.read_text(encoding='mbcs' if os.name == 'nt' else 'utf-8')
        self.assertIn('stand-in.exe', text)
        self.assertIn(f'<"{files.stdin}"', text)
        self.assertIn(f'>"{files.stdout}"', text)
        self.assertIn(f'2>"{files.stderr}"', text)
        self.assertIn('exit_code', text)

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

    def test_a_host_that_stopped_itself_still_gets_its_stderr_parsed(self):
        """The DLL can give up on its own (end of life), which ends the host"""
        session = self._start()
        proc = session['proc']
        proc._alive = False  # the host exited without being asked to
        proc._err.write('dll 1 2 3 ERROR ltk_patcher_dll::entry: end of life reached\n')
        proc._err.flush()

        OverlayManager._stop_ltk_patcher(proc)
        session['reader'].join(timeout=10)

        self.assertTrue(session['session']['eol'])
        self.assertIn('end of life reached', session['session']['error'] or '')

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

    def test_game_ending_before_dll_attaches_is_not_success(self):
        from unittest.mock import MagicMock
        proc = MagicMock()
        proc.poll.return_value = None
        proc.returncode = 0
        reader = MagicMock()
        with patch.object(self.overlay, '_stop_ltk_patcher'), \
                patch.object(self.overlay, '_wipe_overlay_dir'), \
                patch.object(self.overlay, '_log_runoverlay_tail'), \
                patch.object(self.overlay, '_log_patcher_context'):
            code = self.overlay._run_ltk_patcher(
                {'proc': proc, 'session': {'state': 'injecting', 'error': None,
                                          'eol': False, 'attached': False},
                 'reader': reader, 'log': None},
                self.overlay_dir, stop_callback=lambda: True,
            )
        self.assertEqual(code, 1)
        self.assertIn('did not confirm attachment', self.report_issue.call_args.args[2])

    def test_dll_attachment_allows_success(self):
        from unittest.mock import MagicMock
        proc = MagicMock()
        proc.poll.return_value = None
        proc.returncode = 0
        with patch.object(self.overlay, '_stop_ltk_patcher'), \
                patch.object(self.overlay, '_wipe_overlay_dir'):
            code = self.overlay._run_ltk_patcher(
                {'proc': proc, 'session': {'state': 'injected', 'error': None,
                                          'eol': False, 'attached': True},
                 'reader': MagicMock(), 'log': None},
                self.overlay_dir, stop_callback=lambda: True,
            )
        self.assertEqual(code, 0)
        self.report_issue.assert_not_called()

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
            self.assertIsNone(launch_ltk_patcher_host(self.host, command=['stand-in.exe']))

        self.assertEqual(set(Path(tempfile.gettempdir()).glob('dchanger-ltk-*')), before)

    def test_a_busy_shell_does_not_fail_the_launch(self):
        # explorer.exe hands the file to a running instance and may never exit
        # itself; the host's output file is what proves the launch worked.
        launcher = MagicMock()
        launcher.wait.side_effect = subprocess.TimeoutExpired('explorer.exe', 5)

        def fake_wait_for_file(path, timeout):
            path.touch()
            path.with_name('stderr.txt').touch()
            return True

        with patch.object(patcher_launcher.subprocess, 'Popen', return_value=launcher), \
                patch.object(patcher_launcher, '_wait_for_file', side_effect=fake_wait_for_file):
            session = launch_ltk_patcher_host(self.host)

        self.assertIsNotNone(session)
        launcher.wait.assert_not_called()
        launcher.kill.assert_not_called()
        session.close()

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
        with patch.object(self.proc, '_host_exited', return_value=True), \
                patch.object(self.proc, '_started_at', 0.0):
            self.assertEqual(self.proc.poll(), 3)
        self.assertEqual(self.proc.returncode, 3)

    def test_a_host_that_still_holds_its_output_is_running(self):
        self.launcher.poll.return_value = None
        self.assertIsNone(self.proc.poll())

    def test_a_host_is_not_gone_while_it_is_still_starting(self):
        """cmd.exe starts the host as a grandchild, so it shows up late."""
        self.launcher.poll.return_value = 0
        self.assertIsNone(self.proc.poll())


if __name__ == '__main__':
    unittest.main()
