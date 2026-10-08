#!/usr/bin/env python3
"""Manage Stow packages and adjacent user backups using only Python's stdlib."""
import argparse
import contextlib
import io
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def exists(path):
    return os.path.lexists(path)


def files(package):
    for base, dirs, names in os.walk(package):
        for name in dirs + names:
            path = Path(base) / name
            if path.is_symlink():
                raise ValueError(f"Symlinks inside packages are unsupported: {path}")
        for name in names:
            if name == '.stow-local-ignore':
                raise ValueError('Custom Stow ignore rules are unsupported')
            yield Path(base) / name


def managed(target, source):
    return target.is_symlink() and target.resolve() == source.resolve()


def packages_in(repo):
    return {p.name: p for p in repo.iterdir()
            if p.is_dir() and not p.name.startswith(('.', '-'))
            and p.name not in {'tests', 'docs', 'gui', '__pycache__'}}


def repository_links(repo, target, orphan_only=False):
    """Find Stow-shaped links by their stored path, even if the source is gone."""
    found = []
    def walk_error(exc):
        raise exc
    for base, dirs, names in os.walk(target, followlinks=False, onerror=walk_error):
        root = Path(base)
        dirs[:] = [name for name in dirs if root / name != repo]
        for name in dirs + names:
            link = root / name
            if not link.is_symlink() or name.endswith('-backup'):
                continue
            source = Path(os.path.abspath(link.parent / os.readlink(link)))
            try:
                relative = source.relative_to(repo)
            except ValueError:
                continue
            # A Stow link maps repo/package/path to target/path.
            if len(relative.parts) < 2 or Path(*relative.parts[1:]) != link.relative_to(target):
                continue
            if not orphan_only or not source.exists():
                found.append((relative.parts[0], link, source))
    return sorted(found)


def remove_repository_links(repo, target, selected, dry_run=False, orphan_only=False):
    candidates = [entry for entry in repository_links(repo, target, orphan_only) if entry[0] in selected]
    if not candidates:
        print('Подходящие ссылки не найдены.')
        return
    for _, link, source in candidates:
        print(f'Убрать ссылку: {link} → {source}')
    if dry_run:
        return
    lock = repo / '.dotctl.lock'
    try:
        lock.mkdir()
    except FileExistsError:
        raise ValueError(f'Другая операция активна или осталась блокировка: {lock}')
    try:
        for _, link, source in candidates:
            if any(parent.is_symlink() for parent in link.parents if parent != target and target in parent.parents):
                raise ValueError(f'Родитель ссылки изменился: {link}')
            if not link.is_symlink() or Path(os.path.abspath(link.parent / os.readlink(link))) != source:
                raise ValueError(f'Ссылка изменилась после проверки: {link}')
            if orphan_only and source.exists():
                raise ValueError(f'Источник ссылки появился снова: {source}')
            link.unlink()
    finally:
        lock.rmdir()


def confirm(prompt):
    while True:
        answer = input(prompt + ' [Y/n]: ').strip().lower()
        if answer in {'', 'y', 'yes', 'д', 'да'}:
            return True
        if answer in {'n', 'no', 'н', 'нет'}:
            return False
        print('Enter или y — выполнить, n — отменить.')


def package_import_plan(repo, target, name, path):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', name) or name in {'tests', 'docs', 'gui', '__pycache__'}:
        raise ValueError('Имя пакета: латинские буквы, цифры, дефис и подчёркивание; первый символ — буква или цифра.')
    package = repo / name
    if exists(package):
        raise ValueError(f'Пакет уже существует: {package}. Выберите другое имя.')
    source = Path(path).expanduser()
    if not source.is_absolute():
        source = target / source
    source = Path(os.path.abspath(source))
    try:
        relative = source.relative_to(target)
    except ValueError:
        raise ValueError(f'Выберите конфиг внутри каталога {target}')
    if source == target or source == repo or repo in source.parents or source in repo.parents:
        raise ValueError('Нельзя импортировать домашний каталог или сам репозиторий.')
    for item in [source] + list(source.parents):
        if item == target:
            break
        if item.is_symlink():
            raise ValueError(f'Импорт ссылок не поддерживается: {item}')
    if not source.is_file() and not source.is_dir():
        raise ValueError(f'Файл или каталог не найден: {source}')
    candidates = list(files(source)) if source.is_dir() else [source]
    if not candidates:
        raise ValueError('В выбранном каталоге нет файлов конфигурации.')
    for item in candidates:
        if not item.is_file() or item.name == '.stow-local-ignore':
            raise ValueError(f'Нельзя импортировать этот файл: {item}')
    new_paths = {item.relative_to(target) for item in candidates}
    for other in packages_in(repo).values():
        for item in files(other):
            owned = item.relative_to(other)
            if any(owned == new or owned in new.parents or new in owned.parents for new in new_paths):
                raise ValueError(f'Конфиг уже принадлежит пакету {other.name}: {owned}')
    return source, relative, candidates


def import_package(repo, target, name, path):
    source, relative, candidates = package_import_plan(repo, target, name, path)
    lock = repo / '.dotctl.lock'
    try:
        lock.mkdir()
    except FileExistsError:
        raise ValueError(f'Другая операция активна или осталась блокировка: {lock}')
    try:
        with tempfile.TemporaryDirectory(prefix='.dotctl-import-', dir=repo) as staging:
            staged_package = Path(staging) / 'package'
            destination = staged_package / relative
            destination.parent.mkdir(parents=True)
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
            if exists(repo / name):
                raise ValueError(f'Пакет уже существует: {name}')
            staged_package.rename(repo / name)
    finally:
        lock.rmdir()
    return len(candidates)


def clear_screen():
    if sys.stdout.isatty():
        print('\033[2J\033[H', end='', flush=True)


def browse_config(target, repo, render):
    """Browse real files below target without requiring a typed path."""
    current, query, page, message = target, '', 0, ''
    while True:
        try:
            entries = sorted((p for p in current.iterdir()
                              if p != repo and not p.is_symlink()
                              and (p.is_dir() or p.is_file())
                              and query.casefold() in p.name.casefold()),
                             key=lambda p: (not p.is_dir(), p.name != '.config', p.name.casefold()))
        except OSError as exc:
            entries = []
            message = f'Не удалось прочитать каталог: {exc}'
        size = max(5, min(15, shutil.get_terminal_size().lines - 16))
        pages = max(1, (len(entries) + size - 1) // size)
        page = min(page, pages - 1)
        start = page * size
        lines = [f'Каталог: {current}', f'Страница {page + 1}/{pages}']
        if query:
            lines.append(f'Фильтр: {query}')
        lines += [f'  {i + 1}. {p.name}{"/  — открыть папку" if p.is_dir() else "  — выбрать файл"}'
                  for i, p in enumerate(entries[start:start + size], start)]
        if not entries:
            lines.append('  Здесь нет подходящих файлов или папок.')
        lines += ['\n  s. Выбрать текущую папку целиком', '  u. На папку выше',
                  '  h. В домашнюю папку', '  /текст — фильтр по имени; / — сбросить',
                  '  > / < — следующая / предыдущая страница',
                  '  m. Ввести путь вручную', '  0. Отмена']
        render('Главное меню → Добавить пакет → Выбор конфигов', '\n'.join(lines), message)
        answer = input('Номер файла/папки или команда: ').strip()
        message = ''
        if answer == '0':
            return None
        if answer == 'm':
            path = input('Путь к файлу или папке (0 — отмена): ').strip()
            if path == '0':
                continue
            if path:
                return path
            message = 'Путь не должен быть пустым.'
        elif answer == 's':
            if current == target:
                message = 'Выберите папку с конфигами; весь домашний каталог импортировать нельзя.'
            else:
                return str(current)
        elif answer in {'u', 'h'}:
            current = target if answer == 'h' or current == target else current.parent
            query, page = '', 0
        elif answer.startswith('/'):
            query, page = answer[1:], 0
        elif answer in {'>', '<'}:
            page = max(0, min(pages - 1, page + (1 if answer == '>' else -1)))
        elif answer.isdecimal() and start < int(answer) <= min(start + size, len(entries)):
            selected = entries[int(answer) - 1]
            if selected.is_symlink() or not selected.exists():
                message = 'Путь изменился. Выберите другой файл или папку.'
            elif selected.is_dir():
                current, query, page = selected, '', 0
            else:
                return str(selected)
        else:
            message = 'Введите номер из текущей страницы или команду из списка.'


def select_numbers(title, labels, allow_all=False, render=None):
    """Return selected indexes, or an empty list when the user goes back."""
    if not labels:
        print('Ничего не найдено.')
        return []
    lines = [f'  {number}. {label}' for number, label in enumerate(labels, 1)]
    if allow_all:
        lines.append(f'  {len(labels) + 1}. Выбрать всё')
    lines.append('  0. Назад')
    message = ''
    while True:
        if render:
            render(title, '\n'.join(lines), message)
        else:
            print('\n' + title + '\n' + '\n'.join(lines))
            if message:
                print(message)
        answer = input('Номер или несколько номеров через пробел: ').strip()
        if answer == '0':
            return []
        if allow_all and answer == str(len(labels) + 1):
            return list(range(len(labels)))
        parts = answer.split()
        if parts and all(p.isdecimal() and 1 <= int(p) <= len(labels) for p in parts):
            return list(dict.fromkeys(int(p) - 1 for p in parts))
        message = 'Введите номера из списка; 0 — вернуться назад.'


def interactive(repo, target):
    prefix = ['--repo', str(repo), '--target', str(target)]
    menu = '''  1. Мои пакеты               — какие наборы конфигов доступны
  2. Проверить конфиги        — что подключено, а что требует настройки
  3. Подключить все пакеты    — создать ссылки и сохранить старые конфиги
  4. Подключить выбранные     — выбрать пакеты по номерам
  5. Отключить пакеты         — убрать ссылки; бэкапы останутся
  6. Посмотреть бэкапы        — найти сохранённые копии конфигов
  7. Удалить бэкапы пакетов   — выбрать пакеты или удалить все копии
  8. Удалить отдельные бэкапы — выбрать конкретные копии по номерам
  9. Посмотреть, что изменится — проверка подключения без изменения файлов
  10. Добавить пакет          — скопировать конфиг с сохранением вложенных папок
  11. Удалить старые ссылки   — убрать ссылки на удалённые пакеты и файлы
  0. Выход'''
    title, options = 'Главное меню', menu

    def render(page, items, result=''):
        clear_screen()
        print(f'Dotfiles — добро пожаловать!\nПакеты: {repo}\nКонфиги: {target}')
        print(f'\n{page}\n' + '─' * 60)
        print(items)
        print('─' * 60)
        if result:
            print(result)

    def show(result):
        render(title, options, result)

    def wait():
        input('\nНажмите Enter, чтобы вернуться в главное меню…')

    def execute(command):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(prefix + command)
        return output.getvalue().strip()

    def choose(page, labels, allow_all=False):
        nonlocal title, options
        def selection_screen(page, items, result=''):
            nonlocal title, options
            title, options = page, items
            render(page, items, result)
        return select_numbers(page, labels, allow_all, render=selection_screen)

    while True:
        try:
            title, options = 'Главное меню', menu
            show('Выберите действие цифрой. Подключение создаёт ссылки на файлы из пакетов.')
            choice = input('Выберите действие: ').strip()
            if choice == '0':
                return
            if choice == '11':
                stale = repository_links(repo, target, orphan_only=True)
                if not stale:
                    show('Ссылки на удалённые пакеты или файлы не найдены.')
                    wait()
                    continue
                orphan_names = sorted({name for name, _, _ in stale})
                indexes = choose('Главное меню → Удалить старые ссылки', orphan_names, allow_all=True)
                if not indexes:
                    continue
                command = ['prune'] + [orphan_names[index] for index in indexes]
                show('Будут удалены только ссылки; бэкапы сохранятся:\n' + execute(command + ['--dry-run']))
                if confirm('Удалить перечисленные ссылки?'):
                    show('Результат:\n' + execute(command + ['--yes']) + '\n\nГотово.')
                else:
                    show('Отменено. Файлы не изменены.')
                wait()
                continue
            if choice == '10':
                title = 'Главное меню → Добавить пакет'
                options = ('  1. Имя нового пакета, например hypr\n'
                           '  2. Выбор файла или папки по номерам\n'
                           '  3. Проверка структуры и создание пакета\n'
                           '  0. Назад — введите 0 вместо имени или при выборе файла')
                show('Вложенные папки сохраняются относительно каталога конфигов.\n'
                     'Пример: .config/hypr/hyprland.lua → hypr/.config/hypr/hyprland.lua')
                name = input('Имя нового пакета: ').strip()
                if name == '0':
                    continue
                if name in packages_in(repo):
                    indexes = choose('Главное меню → Пакет уже существует',
                                     [f'Подключить существующий пакет {name}',
                                      'Вернуться в главное меню и выбрать другое имя'])
                    if indexes != [0]:
                        continue
                    command = ['apply', name]
                    show('Пакет уже находится в репозитории; копировать его заново не нужно.\n'
                         'Будут выполнены действия:\n' + execute(command + ['--dry-run']))
                    if confirm('Подключить существующий пакет?'):
                        show('Результат:\n' + execute(command) + '\n\nГотово.')
                    else:
                        show('Отменено. Файлы не изменены.')
                    wait()
                    continue
                def browser_screen(page, items, result=''):
                    nonlocal title, options
                    title, options = page, items
                    render(page, f'Новый пакет: {name}\n' + items, result)
                path = browse_config(target, repo, browser_screen)
                if path is None:
                    continue
                title = 'Главное меню → Добавить пакет → Проверка'
                options = f'Пакет: {name}\nИсточник: {path}\nСоздание сохраняет структуру вложенных папок.'
                source, relative, candidates = package_import_plan(repo, target, name, path)
                structure = '\n'.join(f'  {name}/{item.relative_to(target).as_posix()}' for item in candidates)
                show(f'Источник: {source}\nСтруктура нового пакета:\n{structure}\n\n'
                     'Файлы будут скопированы. Исходные конфиги останутся на месте.\n'
                     'Подключение пакета выполняется отдельно через главное меню.')
                if not confirm('Создать пакет?'):
                    show('Создание пакета отменено.')
                else:
                    count = import_package(repo, target, name, path)
                    show(f'Пакет {name} создан: {count} файлов.\n{structure}\n\n'
                         'Выберите «Подключить выбранные» в главном меню для подключения.')
                wait()
                continue
            names = sorted(packages_in(repo))
            if choice == '5':
                names = sorted(set(names) | {name for name, _, _ in repository_links(repo, target)})
            if choice == '1':
                result = '\n'.join(f'  {number}. {name}' for number, name in enumerate(names, 1))
                show('Доступные пакеты:\n' + result if names else 'Пакеты не найдены. Добавьте папку пакета в репозиторий.')
                wait()
                continue
            if choice not in {'2', '3', '4', '5', '6', '7', '8', '9'}:
                show('Введите номер от 0 до 11.')
                wait()
                continue
            if not names:
                show('Пакеты не найдены. Добавьте папку пакета в репозиторий.')
                wait()
                continue
            if choice in {'2', '6'}:
                show(execute(['status' if choice == '2' else 'backups', '--all']))
                wait()
                continue
            if choice == '8':
                # Validate all package paths with the same checks used by clean.
                execute(['backups', '--all'])
                backups = sorted({str(source.relative_to(repo / name))
                                  for name in names for source in files(repo / name)
                                  if exists((target / source.relative_to(repo / name)).with_name(source.name + '-backup'))})
                if not backups:
                    show('Бэкапы не найдены.')
                    wait()
                    continue
                indexes = choose('Главное меню → Удалить отдельные бэкапы',
                                 [name + '-backup' for name in backups])
                if not indexes:
                    continue
                command = ['clean', '--all']
                for index in indexes:
                    command += ['--file', backups[index]]
            else:
                if choice == '3':
                    selection = ['--all']
                else:
                    action_title = {'4': 'Подключить выбранные пакеты', '5': 'Отключить пакеты',
                                    '7': 'Удалить бэкапы пакетов', '9': 'Посмотреть, что изменится'}[choice]
                    indexes = choose('Главное меню → ' + action_title, names, allow_all=True)
                    if not indexes:
                        continue
                    selection = [names[index] for index in indexes]
                action = {'3': 'apply', '4': 'apply', '5': 'remove', '7': 'clean', '9': 'apply'}[choice]
                command = [action] + selection
            preview = execute(command + ['--dry-run'])
            show('Будут выполнены действия:\n' + preview)
            if choice == '9':
                show('Что произойдёт при подключении:\n' + preview + '\n\nПроверка завершена. Файлы не изменены.')
                wait()
                continue
            prompt = ('Удалить перечисленные бэкапы без возможности отмены?'
                      if command[0] == 'clean' else 'Выполнить указанные действия?')
            if not confirm(prompt):
                show('Отменено. Файлы не изменены.')
                wait()
                continue
            result = execute(command + (['--yes'] if command[0] == 'clean' else []))
            show('Результат:\n' + result + '\n\nГотово.')
            wait()
        except (EOFError, KeyboardInterrupt):
            print('\nВыход.')
            return
        except (ValueError, OSError, subprocess.CalledProcessError) as exc:
            show(f'Не удалось выполнить действие: {exc}')
            try:
                wait()
            except (EOFError, KeyboardInterrupt):
                print('\nВыход.')
                return


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--target', type=Path, default=Path.home())
    parser.add_argument('command', nargs='?', choices=['list', 'status', 'apply', 'remove', 'backups', 'clean', 'prune'])
    parser.add_argument('packages', nargs='*')
    parser.add_argument('--all', action='store_true', help='Select every package')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--yes', action='store_true', help='Confirm permanent backup deletion')
    parser.add_argument('--file', action='append', default=[], help='For backups/clean: target-relative config path (repeatable)')
    args = parser.parse_args(argv)
    repo, target = args.repo.expanduser().resolve(), args.target.expanduser().resolve()
    if not repo.is_dir() or not target.is_dir():
        raise ValueError('Repository and target must be existing directories')
    if args.command is None:
        return interactive(repo, target)
    available = packages_in(repo)
    if args.command == 'list':
        print('\n'.join(sorted(available)))
        return
    if args.all and args.packages:
        raise ValueError('Choose package names or --all')
    if args.command in {'remove', 'prune'}:
        if args.file:
            raise ValueError('--file is supported only for backups and clean')
        linked_packages = {name for name, _, _ in repository_links(repo, target, args.command == 'prune')}
        known = set(available) | linked_packages
        selected = known if args.all else set(args.packages)
        if not selected and not args.all:
            raise ValueError('Specify package names or --all')
        if selected - known:
            raise ValueError('Пакеты и ссылки для них не найдены: ' + ', '.join(sorted(selected - known)))
        if args.command == 'prune' and not args.dry_run and not args.yes:
            raise ValueError('Подтвердите удаление старых ссылок через --yes или используйте --dry-run')
        return remove_repository_links(repo, target, selected, args.dry_run, args.command == 'prune')
    selected = sorted(available) if args.all else list(dict.fromkeys(args.packages))
    if not selected:
        raise ValueError('Specify package names or --all')
    unknown = set(selected) - available.keys()
    if unknown:
        raise ValueError(f"Unknown packages: {', '.join(sorted(unknown))}")
    entries, seen = [], set()
    for name in selected:
        if available[name].is_symlink():
            raise ValueError(f'Symlink package is unsupported: {name}')
        for source in sorted(files(available[name])):
            relative = source.relative_to(available[name])
            dest = target / relative
            if dest == repo or repo in dest.parents:
                raise ValueError(f'Package targets the repository: {dest}')
            if dest in seen:
                raise ValueError(f'Packages overlap at {dest}')
            seen.add(dest)
            for parent in dest.parents:
                if parent == target:
                    break
                if parent.is_symlink() or (exists(parent) and not parent.is_dir()):
                    raise ValueError(f'Target parent must be a real directory: {parent}')
            entries.append((source, dest, dest.with_name(dest.name + '-backup')))
    destinations = {dest for _, dest, _ in entries}
    if any(backup in destinations for _, _, backup in entries):
        raise ValueError('A package file collides with a backup path')
    if any(a in b.parents for a in destinations for b in destinations):
        raise ValueError('Packages contain conflicting file and directory paths')
    if args.file:
        if args.command not in {'backups', 'clean'}:
            raise ValueError('--file is supported only for backups and clean')
        requested = set()
        for name in args.file:
            relative = Path(name)
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('--file must be a target-relative config path')
            requested.add(target / relative)
        if not requested <= destinations:
            raise ValueError('--file must name a config from the selected packages')
        entries = [entry for entry in entries if entry[1] in requested]
    if args.command in {'status', 'backups', 'clean'}:
        backups = [backup for _, _, backup in entries if exists(backup)]
        if args.command == 'status':
            for source, dest, _ in entries:
                state = ('Подключён' if managed(dest, source) else
                         'Есть локальный конфиг — при подключении будет создан бэкап' if exists(dest) else 'Не подключён')
                print(f'{state}: {dest}')
            return
        for backup in backups:
            print(backup)
        if not backups:
            print('Бэкапы не найдены.')
        if args.command == 'clean' and not args.dry_run and backups:
            if not args.yes:
                raise ValueError('Permanent deletion requires --yes; preview with --dry-run')
            # Recheck immediately before deletion; never recurse into directories.
            if any(p.is_dir() and not p.is_symlink() for p in backups):
                raise ValueError('Directory backups require manual review; nothing deleted')
            for backup in backups:
                backup.unlink()
        return
    stow = shutil.which('stow')
    if not stow:
        raise ValueError('GNU Stow is required (Ubuntu: sudo apt install stow; Arch: sudo pacman -S stow)')
    for config in {Path.home() / '.stowrc', Path.cwd() / '.stowrc', Path.home() / '.stow-global-ignore'}:
        if exists(config):
            raise ValueError(f'Custom Stow configuration requires manual review: {config}')
    conflicts = [(dest, backup) for source, dest, backup in entries
                 if exists(dest) and not managed(dest, source)]
    if args.command == 'apply':
        for dest, backup in conflicts:
            if dest.is_dir() and not dest.is_symlink():
                raise ValueError(f'Refusing to replace a directory: {dest}')
            if exists(backup):
                raise ValueError(f'Уже есть бэкап: {backup}. Существующий конфиг нельзя сохранить поверх него. '
                                 'Сохраните бэкап в другом месте или удалите через меню, затем повторите подключение.')
            print(f'Сохранить бэкап: {dest} -> {backup}')
    command = [stow, '--dir', str(repo), '--target', str(target), '--no-folding',
               '--ignore=(?!)', '--stow' if args.command == 'apply' else '--delete'] + selected
    print(('Подключить пакеты: ' if args.command == 'apply' else 'Отключить пакеты: ') + ', '.join(selected))
    if args.dry_run:
        for source, dest, _ in entries:
            if args.command == 'apply':
                if managed(dest, source):
                    print(f'Уже подключён, без изменений: {dest}')
                else:
                    print(f'Создать ссылку: {dest} → {source}')
            elif managed(dest, source):
                print(f'Убрать ссылку: {dest}')
            else:
                print(f'Нет ссылки этого пакета, без изменений: {dest}')
        return
    lock = repo / '.dotctl.lock'
    try:
        lock.mkdir()
    except FileExistsError:
        raise ValueError(f'Найдена блокировка операции: {lock}. Это не папка пакета. '
                         'Если другой экземпляр dotctl не запущен, проверьте конфиги после предыдущей операции '
                         'и удалите пустую папку .dotctl.lock перед повторным запуском.')
    moved = []
    original = {dest for _, dest, _ in entries if exists(dest)}
    try:
        if args.command == 'apply':
            for dest, backup in conflicts:
                dest.rename(backup)
                moved.append((dest, backup))
        # Stow's --ignore adds rules instead of replacing built-in defaults.
        # An empty global ignore file gives it the same file set as our planner.
        with tempfile.TemporaryDirectory(prefix='dotctl-stow-') as config_home:
            (Path(config_home) / '.stow-global-ignore').write_text('')
            env = dict(os.environ, HOME=config_home)
            subprocess.run(command, check=True, env=env)
    except (OSError, subprocess.CalledProcessError):
        if args.command == 'apply':
            for source, dest, _ in entries:
                if dest not in original and managed(dest, source):
                    dest.unlink()
            for dest, backup in reversed(moved):
                source = next(s for s, d, _ in entries if d == dest)
                if managed(dest, source):
                    dest.unlink()
                if not exists(dest):
                    backup.rename(dest)
        raise
    finally:
        lock.rmdir()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)
