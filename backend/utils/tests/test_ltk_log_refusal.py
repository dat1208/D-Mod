#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LTK log refusals: the one verdict that only exists after a game has run.

check_mod_health answers 'healthy' for an archive the patcher later refuses,
so the log line is the only evidence. These tests pin the parser to the exact
text a real D-PC log carries.
"""

import tempfile
import unittest
from pathlib import Path

from injection.ltk_runtime import _refusal_for_champion


# Verbatim from ltk-manager.2026-11-24.log on D-PC (LTK Manager 1.33.0).
REAL_REFUSAL = (
    "2026-11-24T19:27:06.634889Z  WARN ltk_manager_runtime::patcher::injector: "
    "[ltk-dll pid=23800 tid=34888 ERROR] ltk_patcher_dll::verify: lazy verification "
    "failed, not overlaying: wad data/final/champions/zed.wad.client: base-skin "
    "check: skin0 is another skin: SkinAudioProperties.BankUnits.Events names "
    "zedSkin<n>\n"
)

HEALTHY_LINES = (
    "2026-11-24T19:27:06.462369Z  INFO ltk_manager_runtime::patcher::injector: "
    "[ltk-dll pid=23800 tid=35204 INFO] ltk_patcher_dll::hooks::fsov::imp: "
    "redirected wad: DATA/FINAL/Maps/Shipping/Map11.wad.client\n"
    "2026-11-24T19:12:15.559393Z  INFO ltk_manager_runtime::patcher::injector: "
    "[ltk-dll pid=22632 tid=22868 INFO] ltk_patcher_dll::entry: joined too late, "
    "not overlaying\n"
)


class RefusalParserTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log = Path(self._tmp.name) / "ltk-manager.2026-11-24.log"

    def tearDown(self):
        self._tmp.cleanup()

    def test_real_refusal_is_read_as_code_and_reason(self):
        self.log.write_text(REAL_REFUSAL, encoding="utf-8")
        code, reason = _refusal_for_champion(self.log, "zed")
        self.assertEqual(code, "c0000229")
        self.assertIn("skin0 is another skin", reason)

    def test_other_champions_are_untouched(self):
        self.log.write_text(REAL_REFUSAL, encoding="utf-8")
        for champion in ("shaco", "ahri", "yasuo"):
            self.assertIsNone(_refusal_for_champion(self.log, champion))

    def test_champion_match_ignores_case(self):
        self.log.write_text(REAL_REFUSAL, encoding="utf-8")
        self.assertIsNotNone(_refusal_for_champion(self.log, "ZED"))

    def test_joined_too_late_is_not_a_skinhack_refusal(self):
        """"joined too late, not overlaying" is a timing problem, not a verdict."""
        self.log.write_text(HEALTHY_LINES, encoding="utf-8")
        self.assertIsNone(_refusal_for_champion(self.log, "zed"))

    def test_missing_log_is_not_a_refusal(self):
        self.assertIsNone(_refusal_for_champion(self.log, "zed"))

    def test_empty_log_is_not_a_refusal(self):
        self.log.write_text("", encoding="utf-8")
        self.assertIsNone(_refusal_for_champion(self.log, "zed"))


if __name__ == "__main__":
    unittest.main()
