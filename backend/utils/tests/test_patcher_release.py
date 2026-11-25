#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Is a newer LTK release published -- the question that decides whether
waiting for a fix is reasonable or pointless.

refresh_patcher answers a different question ("is the DLL expired?") and
answers it correctly. On 2026-11-25 every one of its checks passed while LTK
1.33.0 was blocked by Vanguard: the DLL was valid, signed, and covered the
game build. Re-downloading it changed nothing. These tests pin the version
comparison and the cache, which is what keeps the UI's two-second poll from
turning into thousands of GitHub requests.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from injection import patcher_release as pr


class VersionTests(unittest.TestCase):
    def test_reads_three_part_versions(self):
        self.assertEqual(pr._as_tuple("1.33.0"), (1, 33, 0))
        self.assertEqual(pr._as_tuple("v1.34.2"), (1, 34, 2))

    def test_unparseable_is_none_not_zero(self):
        self.assertIsNone(pr._as_tuple(""))
        self.assertIsNone(pr._as_tuple(None))
        self.assertIsNone(pr._as_tuple("nightly"))

    def test_numeric_not_lexical_order(self):
        # 1.9.0 < 1.10.0 numerically; string compare would say otherwise.
        self.assertLess(pr._as_tuple("1.9.0"), pr._as_tuple("1.10.0"))


class CompareTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "patcher_release.json"
        patcher = mock.patch.object(pr, "state_file", return_value=self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, installed, latest, *, platform="win32"):
        with mock.patch.object(pr, "installed_version", return_value=installed), \
             mock.patch.object(pr, "_fetch_latest", return_value=latest):
            return pr.check(Path("C:/x/ltk-manager.exe"))

    def test_newer_release_is_reported_as_available(self):
        result = self._run("1.33.0", "1.34.0")
        self.assertEqual(result["status"], pr.STATUS_AVAILABLE)
        self.assertEqual(result["installed"], "1.33.0")
        self.assertEqual(result["latest"], "1.34.0")

    def test_same_release_means_waiting_is_pointless(self):
        # The 2026-11-25 situation: no fix exists to download.
        result = self._run("1.33.0", "1.33.0")
        self.assertEqual(result["status"], pr.STATUS_CURRENT)
        self.assertIn("upstream", result["detail"])

    def test_unreachable_github_is_unknown_not_up_to_date(self):
        # Reporting up_to_date here would read as "abandon hope".
        result = self._run("1.33.0", None)
        self.assertEqual(result["status"], pr.STATUS_UNKNOWN)
        self.assertIsNone(result["latest"])

    def test_missing_manager_is_unknown_not_a_comparison(self):
        result = pr.check(None)
        self.assertEqual(result["status"], pr.STATUS_UNKNOWN)
        self.assertIsNone(result["installed"])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "patcher_release.json"
        patcher = mock.patch.object(pr, "state_file", return_value=self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_second_check_hits_the_cache_not_the_network(self):
        with mock.patch.object(pr, "installed_version", return_value="1.33.0"), \
             mock.patch.object(pr, "_fetch_latest", return_value="1.34.0") as fetch:
            pr.check(Path("C:/x/ltk-manager.exe"))
            pr.check(Path("C:/x/ltk-manager.exe"))
        self.assertEqual(fetch.call_count, 1)

    def test_force_skips_the_cache(self):
        with mock.patch.object(pr, "installed_version", return_value="1.33.0"), \
             mock.patch.object(pr, "_fetch_latest", return_value="1.34.0") as fetch:
            pr.check(Path("C:/x/ltk-manager.exe"))
            pr.check(Path("C:/x/ltk-manager.exe"), force=True)
        self.assertEqual(fetch.call_count, 2)

    def test_a_new_install_invalidates_the_cached_answer(self):
        with mock.patch.object(pr, "installed_version", return_value="1.33.0"), \
             mock.patch.object(pr, "_fetch_latest", return_value="1.34.0") as fetch:
            pr.check(Path("C:/x/ltk-manager.exe"))
            with mock.patch.object(pr, "installed_version", return_value="1.34.0"):
                pr.check(Path("C:/x/ltk-manager.exe"))
        self.assertEqual(fetch.call_count, 2)

    def test_stale_cache_expires(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        self.state.write_text(
            '{"schema": 1, "release": {"installed": "1.33.0", '
            '"latest": "1.34.0", "checked_at": "%s"}}' % old,
            encoding="utf-8")
        with mock.patch.object(pr, "installed_version", return_value="1.33.0"), \
             mock.patch.object(pr, "_fetch_latest", return_value="1.34.0") as fetch:
            pr.check(Path("C:/x/ltk-manager.exe"))
        self.assertEqual(fetch.call_count, 1)

    def test_corrupt_cache_is_ignored_not_fatal(self):
        self.state.write_text("{not json", encoding="utf-8")
        with mock.patch.object(pr, "installed_version", return_value="1.33.0"), \
             mock.patch.object(pr, "_fetch_latest", return_value="1.33.0"):
            result = pr.check(Path("C:/x/ltk-manager.exe"))
        self.assertEqual(result["status"], pr.STATUS_CURRENT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
