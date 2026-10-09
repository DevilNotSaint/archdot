import importlib.util
import contextlib
import io
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('dotctl', Path(__file__).resolve().parents[1] / 'dotctl.py')
dotctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dotctl)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.repo, self.home = root / 'repo', root / 'home'
        (self.repo / 'bash').mkdir(parents=True)
        self.home.mkdir()
        (self.repo / 'bash' / '.bashrc').write_text('new')

    def run_cli(self, *args):
        return dotctl.main(['--repo', str(self.repo), '--target', str(self.home), *args])

    def test_backup_collision_keeps_original(self):
        (self.home / '.bashrc').write_text('old')
        (self.home / '.bashrc-backup').write_text('older')
        with patch('dotctl.shutil.which', return_value='/usr/bin/stow'):
            with self.assertRaises(ValueError):
                self.run_cli('apply', 'bash')
        self.assertEqual((self.home / '.bashrc').read_text(), 'old')

    def test_clean_requires_confirmation_and_is_scoped(self):
        backup = self.home / '.bashrc-backup'
        backup.write_text('old')
        other = self.home / '.unrelated-backup'
        other.write_text('keep')
        with self.assertRaises(ValueError):
            self.run_cli('clean', '--all')
        self.assertTrue(backup.exists())
        self.run_cli('clean', 'bash', '--dry-run')
        self.assertTrue(backup.exists())
        self.run_cli('clean', 'bash', '--yes')
        self.assertFalse(backup.exists())
        self.assertTrue(other.exists())

    def test_directory_backup_is_preserved(self):
        backup = self.home / '.bashrc-backup'
        backup.mkdir()
        with self.assertRaises(ValueError):
            self.run_cli('clean', '--all', '--yes')
        self.assertTrue(backup.is_dir())

    def test_clean_single_file(self):
        (self.repo / 'bash' / '.profile').write_text('profile')
        (self.home / '.bashrc-backup').write_text('bash')
        (self.home / '.profile-backup').write_text('profile')
        self.run_cli('clean', 'bash', '--file', '.bashrc', '--yes')
        self.assertFalse((self.home / '.bashrc-backup').exists())
        self.assertTrue((self.home / '.profile-backup').exists())
        with self.assertRaises(ValueError):
            self.run_cli('clean', 'bash', '--file', '../outside', '--yes')

    def test_unknown_package(self):
        with self.assertRaises(ValueError):
            self.run_cli('apply', '../escape')

    def test_menu_invalid_input_and_exit(self):
        output = io.StringIO()
        with patch('builtins.input', side_effect=['invalid', '', '1', '', '0']), contextlib.redirect_stdout(output):
            self.run_cli()
        self.assertIn('Введите номер от 0 до 11', output.getvalue())
        self.assertIn('1. bash', output.getvalue())

    def test_menu_cancel_deletion(self):
        backup = self.home / '.bashrc-backup'
        backup.write_text('keep')
        with patch('builtins.input', side_effect=['7', '1', 'n', '', '0']), contextlib.redirect_stdout(io.StringIO()):
            self.run_cli()
        self.assertEqual(backup.read_text(), 'keep')

    def test_menu_delete_one_selected_backup(self):
        (self.repo / 'bash' / '.profile').write_text('profile')
        (self.home / '.bashrc-backup').write_text('bash')
        (self.home / '.profile-backup').write_text('profile')
        with patch('builtins.input', side_effect=['8', '1', '', '', '0']), contextlib.redirect_stdout(io.StringIO()):
            self.run_cli()
        self.assertFalse((self.home / '.bashrc-backup').exists())
        self.assertTrue((self.home / '.profile-backup').exists())

    def test_confirm_defaults_to_yes_and_retries(self):
        with patch('builtins.input', side_effect=['maybe', '']), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(dotctl.confirm('Продолжить?'))
        with patch('builtins.input', return_value='n'):
            self.assertFalse(dotctl.confirm('Продолжить?'))

    def test_import_nested_file_via_menu(self):
        source = self.home / '.config' / 'hypr' / 'hyprland.lua'
        source.parent.mkdir(parents=True)
        source.write_text('return {}')
        with patch('builtins.input', side_effect=['10', 'hypr', '1', '1', '1', '', '', '0']), contextlib.redirect_stdout(io.StringIO()):
            self.run_cli()
        self.assertEqual((self.repo / 'hypr' / '.config' / 'hypr' / 'hyprland.lua').read_text(), 'return {}')
        self.assertEqual(source.read_text(), 'return {}')
        self.assertFalse(source.is_symlink())

    def test_existing_package_menu_offers_apply_without_import(self):
        with patch('builtins.input', side_effect=['10', 'bash', '1', '', '', '0']), \
                patch.object(dotctl.shutil, 'which', return_value='/usr/bin/stow'), \
                patch.object(dotctl.subprocess, 'run') as run, \
                patch.object(dotctl, 'import_package') as import_mock, \
                contextlib.redirect_stdout(io.StringIO()):
            self.run_cli()
        import_mock.assert_not_called()
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][-2:], ['--stow', 'bash'])
        self.assertEqual((self.repo / 'bash' / '.bashrc').read_text(), 'new')

    def test_apply_lock_reports_lock_path_and_keeps_package(self):
        lock = self.repo / '.dotctl.lock'
        lock.mkdir()
        with patch.object(dotctl.shutil, 'which', return_value='/usr/bin/stow'):
            with self.assertRaisesRegex(ValueError, r'\.dotctl\.lock'):
                self.run_cli('apply', 'bash')
        self.assertTrue(lock.is_dir())
        self.assertTrue((self.repo / 'bash' / '.bashrc').exists())

    def test_status_explains_local_config_without_process_claim(self):
        (self.home / '.bashrc').write_text('local')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.run_cli('status', 'bash')
        self.assertIn('будет создан бэкап', output.getvalue())
        self.assertNotIn('процесс', output.getvalue())

    def test_browser_filter_and_choose_directory(self):
        (self.home / '.config' / 'hypr').mkdir(parents=True)
        (self.home / '.config' / 'other').mkdir()
        screens = []
        with patch('builtins.input', side_effect=['1', '/hypr', '1', 's']):
            selected = dotctl.browse_config(self.home, self.repo, lambda *args: screens.append(args))
        self.assertEqual(Path(selected), self.home / '.config' / 'hypr')
        self.assertIn('Фильтр: hypr', screens[2][1])
        self.assertNotIn('other/', screens[2][1])

    def test_browser_root_selection_and_cancel(self):
        screens = []
        with patch('builtins.input', side_effect=['s', 'u', '0']):
            self.assertIsNone(dotctl.browse_config(self.home, self.repo, lambda *args: screens.append(args)))
        self.assertIn('весь домашний каталог', screens[1][2])
        self.assertIn(str(self.home), screens[2][1])

    def test_browser_parent_navigation_and_manual_path(self):
        (self.home / '.config' / 'hypr').mkdir(parents=True)
        screens = []
        with patch('builtins.input', side_effect=['1', '1', 'u', 'h', 'm', '.config/hypr']):
            selected = dotctl.browse_config(self.home, self.repo, lambda *args: screens.append(args))
        self.assertEqual(selected, '.config/hypr')
        self.assertIn(f'Каталог: {self.home / ".config"}\n', screens[3][1])
        self.assertIn(f'Каталог: {self.home}\n', screens[4][1])

    def test_browser_pagination(self):
        for i in range(7):
            (self.home / f'config{i}').write_text('config')
        screens = []
        with patch('builtins.input', side_effect=['>', '6']), patch.object(dotctl.shutil, 'get_terminal_size', return_value=__import__('os').terminal_size((80, 20))):
            selected = dotctl.browse_config(self.home, self.repo, lambda *args: screens.append(args))
        self.assertEqual(Path(selected).name, 'config5')
        self.assertIn('Страница 2/2', screens[1][1])
        self.assertNotIn('config0', screens[1][1])

    def test_import_directory_preserves_nested_structure(self):
        source = self.home / '.config' / 'hypr'
        (source / 'modules').mkdir(parents=True)
        (source / 'hyprland.lua').write_text('main')
        (source / 'modules' / 'keys.lua').write_text('keys')
        self.assertEqual(dotctl.import_package(self.repo, self.home, 'hypr', str(source)), 2)
        self.assertEqual((self.repo / 'hypr' / '.config' / 'hypr' / 'modules' / 'keys.lua').read_text(), 'keys')
        self.assertFalse((self.repo / '.dotctl.lock').exists())

    def test_import_rejects_existing_package_and_outside_path(self):
        source = self.home / '.bashrc'
        source.write_text('old')
        for name, path in [('bash', str(source)), ('../escape', str(source)), ('fresh', str(self.repo / 'bash' / '.bashrc'))]:
            with self.subTest(name=name, path=path), self.assertRaises(ValueError):
                dotctl.import_package(self.repo, self.home, name, path)
        self.assertEqual(source.read_text(), 'old')

    def test_import_rejects_config_already_owned_by_another_package(self):
        (self.home / '.bashrc').write_text('old')
        with self.assertRaises(ValueError):
            dotctl.import_package(self.repo, self.home, 'other', '.bashrc')
        self.assertFalse((self.repo / 'other').exists())

    def test_import_failure_leaves_no_partial_package(self):
        (self.home / '.newrc').write_text('config')
        with patch.object(dotctl.shutil, 'copy2', side_effect=OSError('copy failed')):
            with self.assertRaises(OSError):
                dotctl.import_package(self.repo, self.home, 'fresh', '.newrc')
        self.assertFalse((self.repo / 'fresh').exists())
        self.assertFalse((self.repo / '.dotctl.lock').exists())
        self.assertFalse(list(self.repo.glob('.dotctl-import-*')))

    def test_menu_error_returns_to_menu(self):
        (self.home / '.bashrc-backup').mkdir()
        output = io.StringIO()
        with patch('builtins.input', side_effect=['7', '1', 'да', '', '0']), contextlib.redirect_stdout(output):
            self.run_cli()
        self.assertIn('Не удалось выполнить действие', output.getvalue())
        self.assertTrue((self.home / '.bashrc-backup').is_dir())

    def test_selection_retries_and_supports_all(self):
        with patch('builtins.input', side_effect=['4', '1 x', '3']), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dotctl.select_numbers('Пакеты', ['bash', 'yazi'], True), [0, 1])

    def test_menu_eof_exits(self):
        with patch('builtins.input', side_effect=EOFError), contextlib.redirect_stdout(io.StringIO()):
            self.run_cli()

    def test_result_keeps_menu_above_and_waits_before_clearing(self):
        output = io.StringIO()
        def answer(prompt):
            if 'Нажмите Enter' in prompt:
                rendered = output.getvalue()
                self.assertLess(rendered.rfind('0. Выход'), rendered.rfind('Доступные пакеты:'))
                self.assertIn('1. bash', rendered)
            return next(answers)
        answers = iter(['1', '', '0'])
        with patch('builtins.input', side_effect=answer), patch.object(dotctl, 'clear_screen') as clear, contextlib.redirect_stdout(output):
            self.run_cli()
        self.assertEqual(clear.call_count, 3)

    def test_submenu_result_keeps_choices_above_result(self):
        (self.home / '.bashrc-backup').write_text('keep')
        screens = []
        output = io.StringIO()
        def record_clear():
            screens.append(output.getvalue())
            output.seek(0)
            output.truncate()
        with patch('builtins.input', side_effect=['7', '1', 'нет', '', '0']), patch.object(dotctl, 'clear_screen', side_effect=record_clear), contextlib.redirect_stdout(output):
            self.run_cli()
        result = next(screen for screen in screens if 'Отменено.' in screen)
        self.assertIn('Главное меню → Удалить бэкапы пакетов', result)
        self.assertLess(result.index('1. bash'), result.index('Отменено.'))
        self.assertTrue((self.home / '.bashrc-backup').exists())

    def test_clear_screen_only_for_terminal(self):
        output = io.StringIO()
        with patch('sys.stdout', output):
            dotctl.clear_screen()
        self.assertEqual(output.getvalue(), '')
        with patch.object(output, 'isatty', return_value=True), patch('sys.stdout', output):
            dotctl.clear_screen()
        self.assertEqual(output.getvalue(), '\033[2J\033[H')

    def test_failed_stow_restores_original(self):
        (self.home / '.bashrc').write_text('old')
        with patch('dotctl.shutil.which', return_value='/usr/bin/stow'), patch('dotctl.subprocess.run', side_effect=OSError('failed')):
            with self.assertRaises(OSError):
                self.run_cli('apply', 'bash')
        self.assertEqual((self.home / '.bashrc').read_text(), 'old')
        self.assertFalse((self.home / '.bashrc-backup').exists())
        self.assertFalse((self.repo / '.dotctl.lock').exists())

    def make_link(self, path, source, directory=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.symlink_to(source, target_is_directory=directory)
        except OSError as exc:
            self.skipTest(f'Symlinks unavailable: {exc}')

    def test_remove_package_that_no_longer_exists(self):
        link = self.home / '.config' / 'hypr' / 'hyprland.lua'
        self.make_link(link, self.repo / 'hypr' / '.config' / 'hypr' / 'hyprland.lua')
        backup = link.with_name(link.name + '-backup')
        backup.write_text('original')
        self.run_cli('remove', 'hypr', '--dry-run')
        self.assertTrue(link.is_symlink())
        self.run_cli('remove', 'hypr')
        self.assertFalse(link.is_symlink())
        self.assertEqual(backup.read_text(), 'original')

    def test_prune_keeps_live_foreign_and_backup_links(self):
        live = self.home / '.bashrc'
        self.make_link(live, self.repo / 'bash' / '.bashrc')
        dead = self.home / '.profile'
        self.make_link(dead, self.repo / 'bash' / '.profile')
        foreign = self.home / '.foreign'
        self.make_link(foreign, self.home / 'missing')
        backup = self.home / '.backup-backup'
        self.make_link(backup, self.repo / 'old' / '.backup-backup')
        with self.assertRaises(ValueError):
            self.run_cli('prune', '--all')
        self.run_cli('prune', '--all', '--yes')
        self.assertFalse(dead.is_symlink())
        for path in [live, foreign, backup]:
            self.assertTrue(path.is_symlink())

    def test_prune_relative_folded_directory_and_does_not_follow_links(self):
        folded = self.home / '.config' / 'hypr'
        source = self.repo / 'hypr' / '.config' / 'hypr'
        relative = __import__('os').path.relpath(source, folded.parent)
        self.make_link(folded, relative, directory=True)
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        hidden = outside / '.profile'
        self.make_link(hidden, self.repo / 'old' / '.profile')
        self.make_link(self.home / 'external', outside, directory=True)
        self.run_cli('prune', '--all', '--yes')
        self.assertFalse(folded.is_symlink())
        self.assertTrue(hidden.is_symlink())

    def test_prune_checks_mapping_and_package_scope(self):
        old = self.home / '.old'
        other = self.home / '.other'
        wrong = self.home / '.wrong'
        self.make_link(old, self.repo / 'old' / '.old')
        self.make_link(other, self.repo / 'other' / '.other')
        self.make_link(wrong, self.repo / 'old' / '.different')
        self.run_cli('prune', 'old', '--yes')
        self.assertFalse(old.is_symlink())
        self.assertTrue(other.is_symlink())
        self.assertTrue(wrong.is_symlink())

    def test_orphan_menu_works_with_no_packages(self):
        (self.repo / 'bash' / '.bashrc').unlink()
        (self.repo / 'bash').rmdir()
        link = self.home / '.old'
        self.make_link(link, self.repo / 'old' / '.old')
        with patch('builtins.input', side_effect=['11', '1', '', '', '0']), contextlib.redirect_stdout(io.StringIO()):
            self.run_cli()
        self.assertFalse(link.is_symlink())

    @unittest.skipUnless(shutil.which('stow'), 'GNU Stow is required')
    def test_real_stow_lifecycle(self):
        nested = self.repo / 'yazi' / '.config' / 'yazi'
        nested.mkdir(parents=True)
        (nested / 'yazi.toml').write_text('config')
        (self.repo / 'bash' / 'README').write_text('package file')
        (self.home / '.bashrc').write_text('old')
        self.run_cli('apply', '--all', '--dry-run')
        self.assertEqual((self.home / '.bashrc').read_text(), 'old')
        self.run_cli('apply', '--all')
        self.assertTrue((self.home / '.bashrc').is_symlink())
        self.assertTrue((self.home / 'README').is_symlink())
        self.assertEqual((self.home / '.bashrc-backup').read_text(), 'old')
        self.assertTrue((self.home / '.config' / 'yazi' / 'yazi.toml').is_symlink())
        self.assertFalse((self.home / '.config').is_symlink())
        self.run_cli('apply', '--all')
        self.run_cli('remove', 'bash')
        self.assertFalse((self.home / '.bashrc').exists())
        self.assertTrue((self.home / '.bashrc-backup').exists())
        self.run_cli('clean', 'bash', '--yes')
        self.assertFalse((self.home / '.bashrc-backup').exists())
        self.assertTrue((self.home / '.config' / 'yazi' / 'yazi.toml').exists())


if __name__ == '__main__':
    unittest.main()
