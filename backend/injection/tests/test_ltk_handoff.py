import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from injection.ltk_handoff import resolve_skin_archive, handoff, find_manager


class HandoffTests(unittest.TestCase):
    def test_resolves_known_taliyah_skin_without_mutating_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "163" / "163011" / "163011.fantome"
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b"example")
            self.assertEqual(resolve_skin_archive(root, "skin_163011", 163), archive)
            self.assertEqual(archive.read_bytes(), b"example")

    def test_missing_skin_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(FileNotFoundError):
                resolve_skin_archive(Path(temp), "skin_163011", 163)

    def test_rejects_bad_id_and_path(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                resolve_skin_archive(Path(temp), "../../bad", 163)
            with self.assertRaises(ValueError):
                resolve_skin_archive(Path(temp), "skin_163011", 0)

    def test_passes_file_as_argv_without_starting_patcher(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            exe = root / "ltk-manager.exe"
            mod = root / "163011.fantome"
            exe.touch(); mod.touch()
            spawn = Mock()
            handoff(mod, exe, spawn=spawn)
            self.assertEqual(spawn.call_args.args[0], [str(exe), str(mod)])
            self.assertEqual(spawn.call_args.kwargs["cwd"], str(root))

    def test_no_manager_is_not_a_success(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(FileNotFoundError):
                find_manager([Path(temp) / "ltk-manager.exe"])
