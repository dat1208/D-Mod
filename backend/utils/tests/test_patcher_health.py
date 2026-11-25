#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Patcher health: the difference between "LTK refused this skin" and
"LTK cannot reach the game at all".

The second one cost matches on 2026-11-25. Vanguard shipped vgk.sys at 00:10
and the patcher DLL stopped loading; every log line still looked healthy --
overlay built, hook installed, game ended clean -- because the DLL never ran
long enough to complain. These tests pin the distinction to the exact log
shape measured on disk.
"""

import tempfile
import unittest
from pathlib import Path

from injection.patcher_health import (
    VERDICT_DLL_ABSENT,
    VERDICT_OK,
    classify_log,
    game_build,
    newest_manager_log,
)

BLOCKED_LOG = """\
2026-11-25T07:47:53.454102Z  INFO ltk_manager_library::overlay: Overlay: game_build=Some("16.23.8418428") meta_schema=false
2026-11-25T07:47:53.469646Z  INFO ltk_manager_library::overlay: Overlay: enabled_mods=1 ids=[8a1c18eb-f947-41de-aebd-56f7ff6b1d05]
2026-11-25T07:47:53.832837Z  INFO ltk_overlay::builder: Overlay build complete: 1 built, 0 reused in 169.1717ms
2026-11-25T07:47:55.428092Z  INFO ltk_manager_runtime::patcher::host::process: [ltk-host] >> config loglevel 32
2026-11-25T07:48:02.578115Z  WARN ltk_manager_runtime::patcher::host::persistent: [ltk-host stderr]    8.689571000s  INFO ltk_patcher_host::worker: game found; hook installed tid=26556 pid=13080
2026-11-25T07:48:20.731606Z  INFO ltk_manager_runtime::patcher::injector: [ltk-host] injecting: scanning for game
2026-11-25T07:48:20.742412Z  INFO ltk_manager_core::patching::pipeline: Game ended clean, no incident recorded
"""

WORKING_LOG = """\
2026-11-24T19:26:53.100000Z  INFO ltk_manager_library::overlay: Overlay: game_build=Some("16.23.8418428") meta_schema=false
2026-11-24T19:26:54.604100Z  INFO ltk_manager_runtime::patcher::host::persistent: [ltk-host stderr]   10.1s  INFO ltk_patcher_host::worker: game found; hook installed tid=35368 pid=23800
2026-11-24T19:26:56.462369Z  INFO ltk_manager_runtime::patcher::injector: [ltk-dll pid=23800 tid=35204 INFO] ltk_patcher_dll::entry: init in process
2026-11-24T19:26:56.462600Z  INFO ltk_manager_runtime::patcher::injector: [ltk-dll pid=23800 tid=35204 INFO] ltk_patcher_dll::hooks::fsov::imp: patched CreateFileA
2026-11-24T19:26:58.702899Z  INFO ltk_manager_runtime::patcher::injector: [ltk-dll pid=23800 tid=35204 INFO] ltk_patcher_dll::hooks::fsov::imp: redirected wad: DATA/FINAL/Champions/Shaco.wad
"""

REFUSAL_LOG = """\
2026-11-24T19:27:02.000000Z  INFO ltk_manager_library::overlay: Overlay: game_build=Some("16.23.8418428") meta_schema=false
2026-11-24T19:27:04.100000Z  INFO ltk_manager_runtime::patcher::host::persistent: [ltk-host stderr]   10.1s  INFO ltk_patcher_host::worker: game found; hook installed tid=22344 pid=23800
2026-11-24T19:27:06.462369Z  INFO ltk_manager_runtime::patcher::injector: [ltk-dll pid=23800 tid=23796 INFO] ltk_patcher_dll::hooks::fsov::imp: lazy verification failed, not overlaying: wad data/final/champions/zed.wad.client: base-skin check: skin0 is another skin
"""


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _log(self, text, name="ltk-manager.2026-11-25.log"):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_hook_without_dll_is_a_silent_block(self):
        verdict, detail = classify_log(self._log(BLOCKED_LOG))
        self.assertEqual(verdict, VERDICT_DLL_ABSENT)
        self.assertIn("13080", detail)

    def test_redirected_wad_proves_the_dll_reached_the_game(self):
        verdict, _ = classify_log(self._log(WORKING_LOG))
        self.assertEqual(verdict, VERDICT_OK)

    def test_a_refusal_still_proves_the_dll_loaded(self):
        # Refusing a skin is a verdict from inside the game. Reporting the
        # patcher as broken here would hide a per-skin problem behind a
        # global-sounding warning.
        verdict, _ = classify_log(self._log(REFUSAL_LOG))
        self.assertEqual(verdict, VERDICT_OK)

    def test_no_game_at_all_returns_none(self):
        # Silence must not become a verdict: warning before any game has run
        # would fire on every fresh install.
        path = self._log(
            '2026-11-25T07:00:00Z  INFO x: Overlay: game_build=Some("16.23.8418428")\n')
        self.assertIsNone(classify_log(path))

    def test_dll_lines_before_the_hook_do_not_count(self):
        # A previous game's DLL output must not excuse the process that
        # followed it.
        text = WORKING_LOG + BLOCKED_LOG
        verdict, _ = classify_log(self._log(text))
        self.assertEqual(verdict, VERDICT_DLL_ABSENT)

    def test_build_is_read_from_the_overlay_line(self):
        path = self._log(BLOCKED_LOG)
        self.assertEqual(game_build(path), "16.23.8418428")

    def test_missing_log_is_not_an_error(self):
        self.assertIsNone(classify_log(self.dir / "nope.log"))
        self.assertIsNone(game_build(self.dir / "nope.log"))

    def test_newest_manager_log_uses_the_date_in_the_name(self):
        (self.dir / "ltk-manager.2026-11-21.log").write_text("a", encoding="utf-8")
        (self.dir / "ltk-manager.2026-11-25.log").write_text("b", encoding="utf-8")
        (self.dir / "ltk-manager.2026-11-24.log").write_text("c", encoding="utf-8")
        (self.dir / "notes.txt").write_text("d", encoding="utf-8")
        # newest_manager_log looks in <roaming>/dev.leaguetoolkit.manager/logs
        logs = self.dir / "dev.leaguetoolkit.manager" / "logs"
        logs.mkdir(parents=True)
        for name in ("ltk-manager.2026-11-21.log",
                     "ltk-manager.2026-11-24.log",
                     "ltk-manager.2026-11-25.log"):
            (logs / name).write_text("x", encoding="utf-8")
        (logs / "ltk-manager.2026-11-30.log.badsession").write_text("y", encoding="utf-8")
        found = newest_manager_log(self.dir)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "ltk-manager.2026-11-25.log")

    def test_no_logs_directory_returns_none(self):
        self.assertIsNone(newest_manager_log(self.dir))


if __name__ == "__main__":
    unittest.main()


    def test_hook_without_dll_is_the_vanguard_block(self):
        path = self._log(BLOCKED_LOG)
        verdict, detail = classify_log(path)
        self.assertEqual(verdict, VERDICT_DLL_ABSENT)
        self.assertIn("13080", detail)

    def test_dll_redirect_is_proof_it_reached_the_game(self):
        path = self._log(WORKING_LOG)
        verdict, detail = classify_log(path)
        self.assertEqual(verdict, VERDICT_OK)
        self.assertIn("23800", detail)

    def test_a_refusal_still_proves_the_dll_ran(self):
        # The skin is broken, but the patcher works: never report the
        # environment as blocked because of one bad archive.
        path = self._log(REFUSAL_LOG)
        verdict, _ = classify_log(path)
        self.assertEqual(verdict, VERDICT_OK)

    def test_no_game_yet_leaves_the_question_open(self):
        path = self._log(
            '2026-11-25T07:47:53.454102Z  INFO overlay: Overlay: '
            'game_build=Some("16.23.8418428")\n'
        )
        self.assertIsNone(classify_log(path))

    def test_only_the_last_hook_decides(self):
        # A broken launch followed by a fixed one must read as fixed.
        mixed = BLOCKED_LOG + WORKING_LOG
        path = self._log(mixed)
        verdict, _ = classify_log(path)
        self.assertEqual(verdict, VERDICT_OK)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_reads_the_build_the_overlay_was_built_for(self):
        path = self.dir / "ltk-manager.2026-11-25.log"
        path.write_text(BLOCKED_LOG, encoding="utf-8")
        self.assertEqual(game_build(path), "16.23.8418428")

    def test_missing_build_is_none_not_a_guess(self):
        path = self.dir / "ltk-manager.2026-11-25.log"
        path.write_text("nothing useful\n", encoding="utf-8")
        self.assertIsNone(game_build(path))


class NewestLogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.roaming = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_picks_the_latest_dated_file_ignoring_suffixes(self):
        logs = self.roaming / "dev.leaguetoolkit.manager" / "logs"
        logs.mkdir(parents=True)
        for name in ("ltk-manager.2026-11-24.log",
                     "ltk-manager.2026-11-25.log",
                     "ltk-manager.2026-11-25.log.badsession",
                     "unrelated.txt"):
            (logs / name).write_text("x", encoding="utf-8")
        found = newest_manager_log(self.roaming)
        self.assertEqual(found.name, "ltk-manager.2026-11-25.log")

    def test_no_logs_directory_is_none(self):
        self.assertIsNone(newest_manager_log(self.roaming))


if __name__ == "__main__":
    unittest.main(verbosity=2)
