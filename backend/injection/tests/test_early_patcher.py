import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from injection.core.manager import InjectionManager
from injection.overlay import overlay_manager
from injection.overlay.overlay_manager import OverlayManager
from injection.tools.tools_manager import ToolsManager


class EarlyPatcherTests(unittest.TestCase):
    """The LTK patcher starts with the game monitor, before the mods are prepared:
    the DLL only overlays games launched after the host started scanning"""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        self.overlay = OverlayManager(root / 'tools', root / 'mods', root / 'game')

        self.host = root / 'tools' / 'ltk_patcher_host.exe'
        patches = [
            patch.object(ToolsManager, 'detect_ltk_patcher', side_effect=lambda: self.host),
            patch.object(overlay_manager, 'check_ltk_patcher', return_value=SimpleNamespace(expired_for=lambda game_dir: False)),
            patch.object(OverlayManager, '_start_ltk_patcher', side_effect=self._fake_session),
            patch.object(OverlayManager, '_abort_ltk_patcher'),
        ]
        self.detect, _, self.start, self.abort = (p.start() for p in patches)
        for p in patches:
            self.addCleanup(p.stop)
        self.running = True

    def _fake_session(self, host_exe, overlay_dir):
        proc = MagicMock()
        proc.poll.side_effect = lambda: None if self.running else 1
        return {'proc': proc, 'session': {}, 'reader': None, 'log': None}

    def test_the_overlay_takes_over_the_early_patcher(self):
        self.overlay.start_patcher_early()
        self.overlay.start_patcher_early()  # once per injection
        self.start.assert_called_once()
        self.assertTrue((self.overlay.mods_dir.parent / 'overlay').is_dir())

        session = self.overlay._take_early_patcher()
        self.assertIs(session['proc'].poll(), None)
        self.assertIsNone(self.overlay._take_early_patcher())
        self.abort.assert_not_called()

    def test_a_patcher_that_died_is_not_taken_over(self):
        self.overlay.start_patcher_early()
        self.running = False
        self.assertIsNone(self.overlay._take_early_patcher())
        self.abort.assert_called_once()

    def test_a_patcher_stopped_after_the_last_game_is_replaced(self):
        self.overlay.start_patcher_early()
        self.running = False  # Rose stopped it at the end of the game
        self.overlay.start_patcher_early()
        self.assertEqual(self.start.call_count, 2)
        self.abort.assert_called_once()

    def test_an_injection_that_gave_up_stops_its_patcher(self):
        self.overlay.start_patcher_early()
        self.overlay.discard_early_patcher()
        self.abort.assert_called_once()
        self.assertIsNone(self.overlay._take_early_patcher())

    def test_expired_patcher_refreshes_before_early_host_starts(self):
        events = []
        self.host.parent.mkdir(parents=True, exist_ok=True)
        self.host.touch()
        with patch.object(overlay_manager, 'check_ltk_patcher', side_effect=[
                SimpleNamespace(expired_for=lambda _: True),
                SimpleNamespace(expired_for=lambda _: False)]), \
                patch.object(overlay_manager.patcher_update, 'refresh_patcher',
                             side_effect=lambda *_: events.append('refresh') or True):
            self.start.side_effect = lambda *args: events.append('host') or self._fake_session(*args)
            self.overlay.start_patcher_early()
        self.assertEqual(events, ['refresh', 'host'])

    def test_expired_patcher_never_starts_when_refresh_fails(self):
        with patch.object(overlay_manager, 'check_ltk_patcher', return_value=
                SimpleNamespace(expired_for=lambda _: True)), \
                patch.object(overlay_manager.patcher_update, 'refresh_patcher', return_value=False), \
                patch.object(self.overlay, '_report_ltk_patcher_eol') as report:
            self.overlay.start_patcher_early()
        self.start.assert_not_called()
        report.assert_called_once()

    def test_nothing_starts_without_a_usable_patcher(self):
        self.host = None
        self.overlay.start_patcher_early()
        self.start.assert_not_called()


class MonitorStartsThePatcherTests(unittest.TestCase):
    """The LTK overlay build is what must be running before the game launches."""

    def test_the_build_starts_with_the_game_monitor(self):
        manager = InjectionManager.__new__(InjectionManager)
        manager.game_monitor = MagicMock()
        manager._ltk_enabled = lambda: True
        manager._ltk_prepare_countdown = MagicMock()

        manager._start_monitor()
        manager.game_monitor.start.assert_called_once()
        manager._ltk_prepare_countdown.assert_called_once()

    def test_no_injector_yet(self):
        """A cold start has no injector; the build still has to run, because
        the countdown reaches T-0 long before one exists."""
        manager = InjectionManager.__new__(InjectionManager)
        manager.game_monitor = MagicMock()
        manager.injector = None
        manager._ltk_enabled = lambda: True
        manager._ltk_prepare_countdown = MagicMock()
        manager._start_monitor()
        manager._stop_monitor()
        manager.game_monitor.start.assert_called_once()
        manager._ltk_prepare_countdown.assert_called_once()


class _InlineThread:
    def __init__(self, target, **_):
        self.target = target

    def start(self):
        self.target()


class _BlockingThread:
    """A thread that stays alive, so is_alive() gates a second start."""

    def __init__(self, target=None, **_):
        self.target = target
        self.started = 0

    def start(self):
        self.started += 1

    def is_alive(self):
        return True


class CountdownStartsThePatcherTests(unittest.TestCase):
    """Started at T-2, the patcher had a few seconds before the client launched
    the game, and a PC that froze for 4s then lost the overlay"""

    def setUp(self):
        self.manager = InjectionManager.__new__(InjectionManager)
        self.manager._initialized = True
        self.manager.injector = MagicMock()
        threads = patch('injection.core.manager.threading', SimpleNamespace(Thread=_InlineThread))
        threads.start()
        self.addCleanup(threads.stop)

    def test_the_countdown_starts_the_patcher(self):
        self.manager._ltk_enabled = lambda: True
        self.manager._ltk_prepare_countdown = MagicMock()
        self.manager.start_patcher_in_background()
        self.manager._ltk_prepare_countdown.assert_called_once()

    def test_a_failed_start_does_not_escape_the_thread(self):
        self.manager._ltk_enabled = lambda: True
        self.manager._ltk_prepare_countdown = MagicMock(side_effect=OSError('denied'))
        self.manager.start_patcher_in_background()  # must not raise

    def test_a_second_tick_does_not_start_a_parallel_build(self):
        """The ticker re-arms once per second; two builds for one skin would
        fight over the LTK staging directory."""
        self.manager._ltk_enabled = lambda: True
        build = MagicMock()
        self.manager._ltk_prepare_countdown = build
        threads = patch('injection.core.manager.threading',
                        SimpleNamespace(Thread=_BlockingThread))
        threads.start()
        self.addCleanup(threads.stop)
        self.manager.start_patcher_in_background()
        self.manager.start_patcher_in_background()
        # Thread still alive from the first call: the second is a no-op.
        self.assertEqual(self.manager._ltk_start_thread.started, 1)

    def test_the_countdown_starts_the_build_before_the_injector_exists(self):
        """A cold start reaches the countdown before _ensure_initialized(), and
        the old guard returned there: the overlay was then only built at T-0s,
        ~3.3s after which the DLL attached and LTK logged "joined too late".
        _ltk_prepare_countdown() resolves the archive from shared_state, so it
        must run with self.injector still None."""
        self.manager._initialized = False
        self.manager.injector = None
        self.manager.shared_state = SimpleNamespace(
            locked_champ_id=None, hovered_champ_id=None,
            selected_chroma_id=None, last_hovered_skin_id=None)
        self.manager._ltk_prepare_countdown = MagicMock()
        self.manager.start_patcher_in_background()
        self.manager._ltk_prepare_countdown.assert_called()

    def test_a_countdown_without_injection_clears_the_build_handle(self):
        """LTK owns its patcher, so discarding only drops D-Changer's handle:
        the next countdown must be free to start a fresh build."""
        stale = MagicMock()
        self.manager._ltk_start_thread = stale
        self.manager.discard_early_patcher()
        self.assertIsNone(self.manager._ltk_start_thread)
        self.assertFalse(stale.stop.called)


if __name__ == '__main__':
    unittest.main()
