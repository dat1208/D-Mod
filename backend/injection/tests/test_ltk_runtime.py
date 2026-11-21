import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from injection.ltk_runtime import LtkRuntime, LtkRuntimeError, _library_id
from injection.core.manager import InjectionManager


class RuntimeTests(unittest.TestCase):
    def test_fingerprint_match_only_when_unambiguous(self):
        self.assertEqual(_library_id([{'id': 'one', 'sourceSha256': 'ABC'}], 'abc'), 'one')
        self.assertIsNone(_library_id([{'id': 'a', 'sourceSha256': 'x'},
                                       {'id': 'b', 'sourceSha256': 'x'}], 'x'))

    def test_prepares_existing_skin_and_starts_official_patcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod = Path(tmp) / '163011.fantome'
            mod.write_bytes(b'a mod')
            bridge = MagicMock()
            bridge.library.return_value = [{'id': 'mod1', 'sourceSha256':
                __import__('hashlib').sha256(b'a mod').hexdigest(), 'enabled': False}]
            bridge.alive.return_value = True
            runtime = LtkRuntime()
            runtime._bridge = bridge
            runtime.prepare(mod)
            bridge.toggle.assert_called_once_with('mod1', True)
            bridge.start_patcher.assert_called_once()
            self.assertTrue(runtime.running())

    def test_does_not_attach_to_uncontrolled_manager(self):
        with patch('injection.ltk_runtime.find_bridge', return_value=None), \
                patch('injection.ltk_runtime._running', return_value=True):
            from injection.ltk_runtime import _connect
            with self.assertRaises(LtkRuntimeError):
                _connect()

    def test_manager_routes_skin_to_ltk_without_legacy_injector(self):
        manager = InjectionManager.__new__(InjectionManager)
        manager.shared_state = MagicMock(current_game_mode='PRACTICETOOL')
        manager.last_skin_name = None
        manager._ltk_archive_path = None
        manager._ltk_runtime = None
        with patch.object(manager, '_ltk_enabled', return_value=True), \
                patch.object(manager, '_ltk_selected_archive', return_value=Path('skin.fantome')), \
                patch.object(manager, '_ltk_prepare') as prepare, \
                patch.object(manager, 'resume_if_suspended'):
            self.assertTrue(manager.inject_skin_immediately('skin_163011', champion_id=163))
        prepare.assert_called_once_with(Path('skin.fantome'))
        self.assertEqual(manager.last_skin_name, 'skin_163011')
