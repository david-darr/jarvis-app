"""Editor contracts without temporary folders or processes. --integration adds real files."""
import hashlib
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['JARVIS_DATA_DIR'] = str(ROOT / 'data' / 'forge3-unit-tests')
from services.forge_sessions import ForgeSessions, ReviewConflict, confined, TEXT_CAP


class EditorTests(unittest.TestCase):
    def service(self):
        service = ForgeSessions.__new__(ForgeSessions)
        service.lock = threading.RLock()
        service.workspace = Mock(return_value=ROOT)
        service._editor_file = Mock(return_value=dict(binary=False, hash='old', mtime='1'))
        return service

    def test_conflicts_before_write_and_binary_refusal(self):
        service = self.service()
        for digest, stamp in [('new', '1'), ('old', '2')]:
            with self.assertRaises(ReviewConflict):
                service.save_file('s', 'README.md', 'edit', digest, stamp)
        service._editor_file.return_value['binary'] = True
        with self.assertRaisesRegex(ValueError, 'read only'):
            service.save_file('s', 'README.md', 'edit', 'old', '1', True)

    def test_confinement_and_content_cap(self):
        for name in ['../x', '.git/config', '.GIT', '/x', 'x\\y', 'x:stream', 'NUL', 'x.', 'dir/../x']:
            with self.subTest(name=name), self.assertRaises(ValueError): confined(ROOT, name)
        with patch('services.forge_sessions._linked', return_value=True), self.assertRaises(ValueError): confined(ROOT, 'linked/file')
        service = self.service()
        for content in ['a\0b', 'é' * TEXT_CAP]:
            with self.assertRaises(ValueError): service.save_file('s', 'README.md', content, 'old', '1')
        service.workspace.assert_not_called()

    def test_snapshot_hash_bytes_and_binary(self):
        path = MagicMock()
        path.is_file.return_value = True
        handle = path.open.return_value.__enter__.return_value
        handle.read.return_value = b'hello\r\n'
        with patch('services.forge_sessions.os.fstat', return_value=Mock(st_mtime_ns=123)):
            result = ForgeSessions._editor_file(path, 'file')
            self.assertEqual(result['hash'], hashlib.sha256(b'hello\r\n').hexdigest())
            self.assertEqual(result['mtime'], '123')
            self.assertEqual(result['content'], 'hello\r\n')
            for body in [b'a\0b', b'\xff']:
                handle.read.return_value = body
                self.assertTrue(ForgeSessions._editor_file(path, 'file')['binary'])

    def test_routes_admin_only_and_409(self):
        source = (ROOT / 'routes/forge_routes.py').read_text(encoding='utf-8')
        self.assertIn("dependencies=[Depends(require_admin)]", source)
        self.assertIn("@router.put('/sessions/{session_id}/file')", source)
        self.assertIn('status_code=409', source)

    def test_utf8_save_and_overwrite_still_rechecks_opened_file(self):
        service = self.service()
        service._editor_file.return_value = dict(binary=False, hash=hashlib.sha256(b'old').hexdigest(), mtime='1')
        path = MagicMock(); handle = path.open.return_value.__enter__.return_value
        handle.read.return_value = b'old'
        with patch('services.forge_sessions.confined', return_value=path), patch('services.forge_sessions.os.fstat', return_value=Mock(st_mtime_ns=1)), patch('services.forge_sessions.os.fsync'):
            saved = service.save_file('s', 'file', 'new é', 'stale', 'stale', True)
            handle.write.assert_called_once_with('new é'.encode('utf-8'))
            self.assertEqual(saved['hash'], hashlib.sha256('new é'.encode('utf-8')).hexdigest())
            handle.read.return_value = b'agent changed again'
            with self.assertRaises(ReviewConflict): service.save_file('s', 'file', 'edit', 'stale', 'stale', True)


if '--integration' in sys.argv:
    sys.argv.remove('--integration')
    from test_forge_sessions import ForgeSessionTests
    class EditorFileTests(ForgeSessionTests):
        def test_editor_real_save_and_conflict(self):
            session = self.create(); sid = session['id']
            before = self.service.file(sid, 'code.txt')
            self.service.save_file(sid, 'code.txt', 'human edit\n', before['hash'], before['mtime'])
            self.assertEqual(self.service.file(sid, 'code.txt')['content'], 'human edit\n')
            with self.assertRaises(ReviewConflict): self.service.save_file(sid, 'code.txt', 'stale', before['hash'], before['mtime'])
            self.service.save_file(sid, 'code.txt', 'overwrite', before['hash'], before['mtime'], True)
            (Path(session['workspace_dir']) / 'binary').write_bytes(b'\0')
            self.assertTrue(self.service.file(sid, 'binary')['binary'])
            with self.assertRaises(ValueError): self.service.save_file(sid, 'binary', 'text', '', '', True)
            for name in ('../outside', '.git', '.git/config', '.GIT/config', 'dir/../outside'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    self.service.save_file(sid, name, 'edit', '', '', True)
            with patch('services.forge_sessions._linked', side_effect=lambda path: path.name == 'code.txt'):
                with self.assertRaises(ValueError): self.service.save_file(sid, 'code.txt', 'edit', '', '', True)

if __name__ == '__main__': unittest.main()
