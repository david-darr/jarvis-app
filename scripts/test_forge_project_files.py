"""Project Explorer stays read-only and uses the session path and text guards."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
environment = tempfile.TemporaryDirectory(prefix='forge-explorer-data-')
os.environ['JARVIS_DATA_DIR'] = str(Path(environment.name) / 'app-data')
from services import forge_project_files as files
from core import session_manager_store


def tearDownModule():
    session_manager_store.close()
    environment.cleanup()


class ProjectFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='forge-explorer-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = dict(path=str(self.root))
        (self.root / 'src').mkdir()
        # newline='': the viewer returns stored bytes, so store exactly LF on Windows too.
        (self.root / 'src' / 'code.py').write_text('print("hello")\n', encoding='utf-8', newline='')
        (self.root / 'README.md').write_text('# Project\n', encoding='utf-8')
        (self.root / '.git').mkdir()
        (self.root / '.git' / 'config').write_text('secret', encoding='utf-8')

    def test_plain_folder_lazy_tree_and_text(self):
        with patch.object(files.forge_git, 'run_git', return_value=(128, 'not git')):
            rows = files.project_files(self.project)['entries']
            self.assertEqual([r['path'] for r in rows], ['src', 'README.md'])
            nested = files.project_files(self.project, 'src')['entries']
            self.assertEqual([r['path'] for r in nested], ['src/code.py'])
        self.assertEqual(files.project_file(self.project, 'src/code.py')['content'], 'print("hello")\n')

    def test_git_tree_omits_ignored_and_missing_files(self):
        (self.root / 'ignored.txt').write_text('ignored', encoding='utf-8')
        (self.root / 'new.txt').write_text('new', encoding='utf-8')
        with patch.object(files.forge_git, 'run_git', side_effect=[(0, 'src/code.py\0README.md\0deleted.txt\0'), (0, 'new.txt\0')]):
            self.assertEqual([r['path'] for r in files.project_files(self.project)['entries']], ['src', 'new.txt', 'README.md'])

    def test_unsafe_paths_links_binary_and_caps_are_refused(self):
        for path in ('../escape', '.git/config', 'C:/escape', 'src/../README.md', 'file:stream', 'NUL'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                files.project_file(self.project, path)
        (self.root / 'binary.bin').write_bytes(b'\0secret')
        (self.root / 'large.txt').write_bytes(b'x' * (1024 * 1024 + 1))
        for path in ('binary.bin', 'large.txt'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                files.project_file(self.project, path)
        with patch('services.forge_sessions._linked', side_effect=lambda path: path.name == 'src'):
            with self.assertRaisesRegex(ValueError, 'Links'):
                files.project_file(self.project, 'src/code.py')
            with patch.object(files.forge_git, 'run_git', return_value=(128, 'not git')):
                self.assertNotIn('src', [r['path'] for r in files.project_files(self.project)['entries']])


if __name__ == '__main__':
    unittest.main()
