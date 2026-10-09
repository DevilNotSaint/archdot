from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import patch

from gui import model


class GuiModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'repo'
        self.target = Path(self.temp.name) / 'home'
        (self.repo / 'bash').mkdir(parents=True)
        (self.repo / 'gui').mkdir()
        self.target.mkdir()
        (self.repo / 'bash' / '.bashrc').write_text('new')

    def test_snapshot_reports_local_config_and_backup(self):
        (self.target / '.bashrc').write_text('local')
        (self.target / '.bashrc-backup').write_text('original')
        data = model.snapshot(self.repo, self.target)
        self.assertEqual(len(data['packages']), 1)
        self.assertEqual(data['packages'][0]['local'], 1)
        self.assertEqual(data['packages'][0]['state'], 'Не подключён')
        self.assertEqual(data['backups'][0]['relative'], '.bashrc')
        self.assertEqual((self.target / '.bashrc').read_text(), 'local')

    def test_command_propagates_backend_error(self):
        result = subprocess.CompletedProcess([], 1, '', 'Backup already exists')
        with patch.object(model.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(ValueError, 'Backup already exists'):
                model.command(self.repo, self.target, ['apply', 'bash'])

    def test_adapter_uses_structured_arguments_and_utf8(self):
        result = subprocess.CompletedProcess([], 0, 'Готово', '')
        with patch.object(model.subprocess, 'run', return_value=result) as run:
            output = model.command(self.repo, self.target, ['apply', 'bash', '--dry-run'])
        self.assertEqual(output, 'Готово')
        self.assertIn(str(self.repo), run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['encoding'], 'utf-8')
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_missing_paths_are_reported(self):
        with self.assertRaises(ValueError):
            model.snapshot(self.repo / 'missing', self.target)

    @unittest.skipUnless(shutil.which('stow'), 'GNU Stow is required')
    def test_real_adapter_stow_lifecycle(self):
        config = self.target / '.bashrc'
        config.write_text('local')
        preview = model.command(self.repo, self.target, ['apply', 'bash', '--dry-run'])
        self.assertIn('Создать ссылку', preview)
        self.assertEqual(config.read_text(), 'local')
        model.command(self.repo, self.target, ['apply', 'bash'])
        self.assertTrue(config.is_symlink())
        self.assertEqual(model.snapshot(self.repo, self.target)['packages'][0]['state'], 'Подключён')
        model.command(self.repo, self.target, ['remove', 'bash'])
        self.assertFalse(config.is_symlink())
        self.assertEqual((self.target / '.bashrc-backup').read_text(), 'local')
