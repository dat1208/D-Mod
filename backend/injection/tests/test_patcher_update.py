import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from injection.tools import patcher_update
from injection.tools.patcher import LTK_PATCHER_DLL, LTK_PATCHER_HOST


class RefreshPatcherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.tools = self.root / 'tools'
        self.tools.mkdir()
        self.game = self.root / 'game'
        self.game.mkdir()
        self.appdata = self.root / 'appdata'
        self.appdata.mkdir()
        (self.tools / LTK_PATCHER_HOST).write_bytes(b'old host')
        (self.tools / LTK_PATCHER_DLL).write_bytes(b'old dll')

    def _stage(self, name='digest', dll=b'new dll', host=b'new host'):
        """Put a DLL where LTK Manager would stage it."""
        d = self.appdata / patcher_update.LTK_MANAGER_ID / 'patcher' / hashlib.sha256(dll).hexdigest()
        d.mkdir(parents=True)
        (d / LTK_PATCHER_DLL).write_bytes(dll)
        (d / LTK_PATCHER_HOST).write_bytes(host)
        (d.parent / 'delivery.json').write_text('{}', encoding='utf-8')
        return d / LTK_PATCHER_DLL

    def _refresh(self, installed_eol, game_build, new_eol, staged=True, run_ok=True):
        def fake_dll_eol(path):
            return new_eol if path.parent != self.tools else installed_eol

        def fake_run(exe, timeout):
            return self._stage('run') if run_ok else None

        with patch.dict('os.environ', {'APPDATA': str(self.appdata)}), \
                patch.object(patcher_update, 'read_dll_eol', side_effect=fake_dll_eol), \
                patch.object(patcher_update, '_require_valid_signature'), \
                patch.object(patcher_update, 'read_game_build', return_value=game_build), \
                patch.object(patcher_update, '_manager_exe', return_value=Path('ltk-manager.exe')), \
                patch.object(patcher_update, '_run_manager_until_dll', side_effect=fake_run):
            if staged:
                self._stage()
            return patcher_update.refresh_patcher(self.tools, self.game)

    def test_rejects_staged_dll_without_manifest(self):
        dll = self._stage()
        (dll.parent.parent / 'delivery.json').unlink()
        with self.assertRaises(patcher_update.PatcherUpdateError):
            patcher_update._verify_staged_dll(dll)

    def test_rejects_staged_dll_when_digest_does_not_match(self):
        dll = self._stage()
        dll.write_bytes(b'tampered')
        with self.assertRaises(patcher_update.PatcherUpdateError):
            patcher_update._verify_staged_dll(dll)

    def test_rejects_unsigned_staged_dll(self):
        dll = self._stage()
        with patch.object(patcher_update, 'read_dll_eol', return_value=3000), \
                patch.object(patcher_update, '_require_valid_signature',
                             side_effect=patcher_update.PatcherUpdateError('NotSigned')):
            with self.assertRaises(patcher_update.PatcherUpdateError):
                patcher_update._verify_staged_dll(dll)

    def test_a_patcher_that_covers_the_game_is_left_alone(self):
        self.assertFalse(self._refresh(installed_eol=2000, game_build=1000, new_eol=3000))
        self.assertEqual((self.tools / LTK_PATCHER_DLL).read_bytes(), b'old dll')

    def test_a_staged_dll_is_copied_over_an_expired_one(self):
        self.assertTrue(self._refresh(installed_eol=1000, game_build=2000, new_eol=3000))
        self.assertEqual((self.tools / LTK_PATCHER_DLL).read_bytes(), b'new dll')
        self.assertEqual((self.tools / LTK_PATCHER_HOST).read_bytes(), b'new host')

    def test_the_previous_pair_is_kept_as_a_backup(self):
        self._refresh(installed_eol=1000, game_build=2000, new_eol=3000)
        self.assertEqual((self.tools / (LTK_PATCHER_DLL + '.bak')).read_bytes(), b'old dll')

    def test_a_staged_dll_that_is_still_too_old_is_not_installed(self):
        self.assertFalse(self._refresh(installed_eol=1000, game_build=3000, new_eol=2000))
        self.assertEqual((self.tools / LTK_PATCHER_DLL).read_bytes(), b'old dll')

    def test_manager_is_run_when_nothing_is_staged(self):
        self.assertTrue(self._refresh(installed_eol=1000, game_build=2000, new_eol=3000, staged=False))
        self.assertEqual((self.tools / LTK_PATCHER_DLL).read_bytes(), b'new dll')

    def test_a_manager_that_downloads_nothing_leaves_the_pair_alone(self):
        self.assertFalse(self._refresh(installed_eol=1000, game_build=2000, new_eol=3000,
                                       staged=False, run_ok=False))
        self.assertEqual((self.tools / LTK_PATCHER_DLL).read_bytes(), b'old dll')

    def test_an_unknown_build_accepts_the_staged_dll(self):
        self.assertTrue(self._refresh(installed_eol=None, game_build=None, new_eol=3000))


class StagedDllsTests(unittest.TestCase):
    def test_the_newest_staged_dll_comes_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / patcher_update.LTK_MANAGER_ID / 'patcher'
            old = root / 'a'
            new = root / 'b'
            for d in (old, new):
                d.mkdir(parents=True)
                (d / LTK_PATCHER_DLL).write_bytes(b'x')
            import os
            os.utime(old / LTK_PATCHER_DLL, (1000, 1000))
            os.utime(new / LTK_PATCHER_DLL, (2000, 2000))
            with patch.dict('os.environ', {'APPDATA': str(Path(tmp))}):
                self.assertEqual(patcher_update.staged_dlls()[0].parent.name, 'b')

    def test_no_data_dir_means_nothing_is_staged(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict('os.environ', {'APPDATA': tmp}):
            self.assertEqual(patcher_update.staged_dlls(), [])


if __name__ == '__main__':
    unittest.main()
