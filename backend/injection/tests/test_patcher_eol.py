import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from injection.tools.patcher import (
    LTK_PATCHER_DLL,
    read_dll_eol,
    write_eol_sidecar,
)


class EolFromManifestTests(unittest.TestCase):
    """A staged DLL's date comes from the manifest LTK Manager writes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.staged = self.root / 'patcher' / 'digest'
        self.staged.mkdir(parents=True)
        self.dll = self.staged / LTK_PATCHER_DLL
        self.dll.write_bytes(b'not a real PE file')

    def _write_manifest(self, digest, eol, channel='stable'):
        body = json.dumps({'schema': 1, 'channel': channel, 'sequence': 2,
                           'release': {'version': 'ltk_patcher-26', 'digest': digest,
                                       'size': 1, 'protocol': 3,
                                       'managers': '>=1.30.0', 'eol': eol},
                           'revoked': []}).encode()
        (self.root / 'patcher' / 'delivery.json').write_text(json.dumps({
            'channels': {channel: {'envelope': {
                'manifest': base64.b64encode(body).decode(),
                'signature': 'untrusted comment: x',
            }}}
        }), encoding='utf-8')

    def test_the_manifest_supplies_the_date(self):
        self._write_manifest(hashlib.sha256(self.dll.read_bytes()).hexdigest(), 1794988800)
        self.assertEqual(read_dll_eol(self.dll), 1794988800)

    def test_a_manifest_for_another_dll_is_ignored(self):
        self._write_manifest('0' * 64, 1794988800)
        self.assertIsNone(read_dll_eol(self.dll))

    def test_a_missing_manifest_is_not_an_error(self):
        self.assertIsNone(read_dll_eol(self.dll))


class EolSidecarTests(unittest.TestCase):
    """A DLL copied out of staging records its date beside it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dll = Path(self._tmp.name) / LTK_PATCHER_DLL
        self.dll.write_bytes(b'not a real PE file')

    def test_the_recorded_date_is_read_back(self):
        write_eol_sidecar(self.dll, 1794988800)
        self.assertEqual(read_dll_eol(self.dll), 1794988800)

    def test_an_unknown_date_removes_the_record(self):
        write_eol_sidecar(self.dll, 1794988800)
        write_eol_sidecar(self.dll, None)
        self.assertIsNone(read_dll_eol(self.dll))


if __name__ == '__main__':
    unittest.main()
