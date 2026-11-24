#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Blocked-skins blacklist: records a refusal, locks it, forgets on repair."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from injection import blocked_skins


class BlockedSkinsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.patcher = mock.patch.object(
            blocked_skins, "get_state_dir", return_value=self.state)
        self.patcher.start()
        self.archive = self.state / "skins" / "238" / "238068" / "238068.fantome"
        self.archive.parent.mkdir(parents=True)
        self.archive.write_bytes(b"PK\x03\x04 fake fantome v1")

    def tearDown(self):
        self.patcher.stop()
        self._tmp.cleanup()

    def test_unknown_archive_is_not_blocked(self):
        self.assertIsNone(blocked_skins.is_blocked(self.archive))

    def test_refusal_is_recorded_and_reported(self):
        digest = blocked_skins.record_refusal(
            self.archive, code="c0000229", reason="skin0 is another skin",
            skin_id=238068)
        self.assertIsNotNone(digest)
        record = blocked_skins.is_blocked(self.archive)
        self.assertEqual(record["code"], "c0000229")
        self.assertEqual(record["reason"], "skin0 is another skin")
        self.assertEqual(record["skinId"], 238068)
        self.assertEqual(record["timesRefused"], 1)

    def test_second_refusal_increments_the_count(self):
        blocked_skins.record_refusal(self.archive, reason="first")
        blocked_skins.record_refusal(self.archive, reason="second")
        self.assertEqual(blocked_skins.is_blocked(self.archive)["timesRefused"], 2)

    def test_repairing_the_archive_clears_the_old_verdict(self):
        blocked_skins.record_refusal(self.archive, reason="broken")
        self.archive.write_bytes(b"PK\x03\x04 repaired fantome v2")
        self.assertIsNone(blocked_skins.is_blocked(self.archive))

    def test_forget_removes_the_record(self):
        blocked_skins.record_refusal(self.archive)
        self.assertTrue(blocked_skins.forget(self.archive))
        self.assertIsNone(blocked_skins.is_blocked(self.archive))

    def test_verdict_from_another_game_version_is_ignored(self):
        blocked_skins.record_refusal(self.archive, game_version="16.23")
        self.assertIsNone(blocked_skins.is_blocked(self.archive, game_version="16.24"))
        self.assertIsNotNone(blocked_skins.is_blocked(self.archive, game_version="16.23"))

    def test_corrupt_state_never_locks_anything(self):
        blocked_skins.record_refusal(self.archive, reason="x")
        blocked_skins.state_file().write_text("{ this is not json", encoding="utf-8")
        self.assertIsNone(blocked_skins.is_blocked(self.archive))
        self.assertEqual(blocked_skins.all_blocked(), {})

    def test_missing_archive_is_not_hashed(self):
        missing = self.state / "nope.fantome"
        self.assertIsNone(blocked_skins.archive_digest(missing))
        self.assertIsNone(blocked_skins.record_refusal(missing))


if __name__ == "__main__":
    unittest.main()
