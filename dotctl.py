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


def walk_error(exc):
    raise exc


@contextlib.contextmanager
def operation_lock(repo):
    lock = repo / '.dotctl.lock'
    try:
        lock.mkdir()
    except FileExistsError:
        raise ValueError(f'Другая операция активна или осталась блокировка: {lock}') from None
    try:
        yield
    finally:
        lock.rmdir()


def files(package):
    for base, dirs, names in os.walk(package, onerror=walk_error):
        dirs[:] = [name for name in dirs if not name.endswith('-backup')]
        names = [name for name in names if not name.endswith('-backup')]
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


def connected_packages(repo, target):
    """Only nonempty packages whose every file is linked are connected."""
    connected = set()
    for name, folder in packages_in(repo).items():
        if folder.is_symlink():
            continue
        try:
            sources = list(files(folder))
            if sources and all(managed(target / source.relative_to(folder), source) for source in sources):
                connected.add(name)
        except (OSError, ValueError, RuntimeError):
            continue
    return connected


def packages_in(repo):
    return {p.name: p for p in repo.iterdir()
            if p.is_dir() and not p.name.startswith(('.', '-'))
            and p.name not in {'tests', 'docs', 'gui', '__pycache__'}}


def repository_links(repo, target, orphan_only=False):
    """Find Stow-shaped links by their stored path, even if the source is gone."""
    found = []
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
    with operation_lock(repo):
        for _, link, source in candidates:
            if any(parent.is_symlink() for parent in link.parents if parent != target and target in parent.parents):
                raise ValueError(f'Родитель ссылки изменился: {link}')
            if not link.is_symlink() or Path(os.path.abspath(link.parent / os.readlink(link))) != source:
                raise ValueError(f'Ссылка изменилась после проверки: {link}')
            if orphan_only and source.exists():
                raise ValueError(f'Источник ссылки появился снова: {source}')
            link.unlink()


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
    if any(part.endswith('-backup') for part in relative.parts):
        raise ValueError('Бэкапы с суффиксом -backup нельзя импортировать как конфиги.')
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
        if not item.is_file() or item.name == '.stow-local-ignore' or item.name.endswith('-backup'):
            raise ValueError(f'Нельзя импортировать этот файл: {item}')
    new_paths = {item.relative_to(target) for item in candidates}
    for other in packages_in(repo).values():
        for item in files(other):
            owned = item.relative_to(other)
            if any(owned == new or owned in new.parents or new in owned.parents for new in new_paths):
                raise ValueError(f'Конфиг уже принадлежит пакету {other.name}: {owned}')
    return source, relative, candidates


def import_package(repo, target, name, path):
    with operation_lock(repo):
        source, relative, candidates = package_import_plan(repo, target, name, path)
        with tempfile.TemporaryDirectory(prefix='.dotctl-import-', dir=repo) as staging:
            staged_package = Path(staging) / 'package'
            destination = staged_package / relative
            destination.parent.mkdir(parents=True)
            if source.is_dir():
                shutil.copytree(source, destination, ignore=shutil.ignore_patterns('*-backup'))
            else:
                shutil.copy2(source, destination)
            if exists(repo / name):
                raise ValueError(f'Пакет уже существует: {name}')
            staged_package.rename(repo / name)
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


def select_actions(page, labels, render, verb='Подключить', connected=None):
    """Space toggles rows; Enter applies the selection or a typed command."""
    selected = []
    cursor = 0
    typed = ''
    message = ''
    terminal = sys.stdin.isatty() and sys.stdout.isatty()
    color = sys.stdout.isatty() and 'NO_COLOR' not in os.environ
    def caption(i, label):
        if connected is None or not color:
            return label
        return f'\033[{32 if i in connected else 31}m{label}\033[0m'
    def screen():
        items = '\n'.join(f'{"›" if terminal and i == cursor else " "} {i + 1}. [{"✓" if i in selected else " "}] {caption(i, label)}'
                          for i, label in enumerate(labels))
        items += (f'\n\n↑/↓ — курсор · Пробел — отметить · Enter — {verb.lower()} выбранные'
                  '\n-a — все · 1 2 5 -s — выбранные по номерам · 0 / Esc — назад')
        if connected is not None:
            items += ' · Зелёный — подключён, красный — не подключён полностью'
        return items
    with terminal_keys() if terminal else contextlib.nullcontext() as read_key:
        while True:
            render(page, screen(), message + (f'\nКоманда: {typed}' if typed else ''))
            if terminal:
                key = read_key()
                if key in ('up', 'down'):
                    cursor = (cursor + (1 if key == 'down' else -1)) % max(1, len(labels))
                    continue
                if key in ('\x03', '\x04'):
                    raise KeyboardInterrupt
                if key == '\x1b':
                    return []
                if key in ('\x7f', '\b'):
                    typed = typed[:-1]
                    continue
                if key == ' ' and not typed:
                    if labels:
                        if cursor in selected:
                            selected.remove(cursor)
                        else:
                            selected.append(cursor)
                    message = ''
                    continue
                if key not in ('\r', '\n'):
                    if key and key in '0123456789 -as':
                        typed += key
                    continue
                parts = typed.strip().split()
                typed = ''
            else:
                parts = input('Выбор: ').strip().split()
            if parts == ['0']:
                return []
            if parts == ['-a']:
                selected = list(range(len(labels)))
                if selected:
                    render(page, screen(), 'Выбраны все пункты.')
                    return selected
            apply_selected = not parts or parts[-1] == '-s'
            if parts and parts[-1] == '-s':
                parts = parts[:-1]
            if parts:
                if not all(p.isdecimal() and 1 <= int(p) <= len(labels) for p in parts):
                    message = 'Введите номера из списка, -s, -a или 0.'
                    continue
                selected = list(dict.fromkeys(int(p) - 1 for p in parts))
            if apply_selected and selected:
                render(page, screen(), 'Выбрано: ' + ', '.join(labels[i] for i in selected))
                return selected
            message = 'Сначала выберите пункты.' if apply_selected else ''


@contextlib.contextmanager
def terminal_keys():
    """Read terminal keys and always restore the original terminal mode."""
    with contextlib.ExitStack() as stack:
        if os.name == 'nt':
            import msvcrt

            def read_key():
                key = msvcrt.getwch()
                if key in ('\x00', '\xe0'):
                    return {'H': 'up', 'P': 'down'}.get(msvcrt.getwch(), '')
                return key
        else:
            import select
            import termios
            import tty
            fd = sys.stdin.fileno()
            saved = termios.tcgetattr(fd)
            stack.callback(termios.tcsetattr, fd, termios.TCSADRAIN, saved)
            tty.setcbreak(fd)

            def read_key():
                data = os.read(fd, 1)
                if not data:
                    raise EOFError
                key = data.decode()
                if key == '\x1b':
                    sequence = ''
                    while select.select([fd], [], [], 0.05)[0]:
                        part = os.read(fd, 1)
                        if not part:
                            break
                        sequence += part.decode()
                        if sequence[-1].isalpha():
                            break
                    return {'[A': 'up', '[B': 'down', 'OA': 'up', 'OB': 'down'}.get(
                        sequence, '' if sequence else key)
                return key
        yield read_key


def action_choice(menu, render, page='Главное меню'):
    """Arrow navigation on a terminal; retain numeric input for pipes/scripts."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return input('Выберите действие: ').strip()
    lines = menu.splitlines()
    selected, digits = 0, ''
    with terminal_keys() as read_key:
        while True:
            marked = '\n'.join(('› ' if i == selected else '  ') + line.strip()
                               for i, line in enumerate(lines))
            render(page, marked,
                   '↑/↓ — выбор, Enter — выполнить; можно ввести номер: ' + digits)
            key = read_key()
            if key in ('up', 'down'):
                selected = (selected + (1 if key == 'down' else -1)) % len(lines)
                digits = ''
            elif key in ('\r', '\n'):
                return digits or lines[selected].strip().split('.', 1)[0]
            elif key in ('\x03', '\x04'):
                raise KeyboardInterrupt
            elif key == '\x1b':
                return '0'
            elif key in ('\x7f', '\b'):
                digits = digits[:-1]
            elif key.isdecimal():
                digits += key


def interactive(repo, target):
    prefix = ['--repo', str(repo), '--target', str(target)]
    menu = '''  1. Подключить пакеты        — выбрать один, несколько или все
  2. Проверить конфиги        — что подключено, а что требует настройки
  3. Отключить пакеты         — убрать ссылки; бэкапы останутся
  4. Посмотреть бэкапы        — найти сохранённые копии конфигов
  5. Очистка                  — удалить бэкапы или старые ссылки
  6. Посмотреть, что изменится — проверка подключения без изменения файлов
  7. Добавить пакет           — скопировать конфиг с сохранением вложенных папок
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

    def choose(page, labels, allow_all=False, actions=False, verb='Подключить', connected=None):
        nonlocal title, options
        def selection_screen(page, items, result=''):
            nonlocal title, options
            title, options = page, items
            render(page, items, result)
        if actions:
            return select_actions(page, labels, selection_screen, verb, connected)
        items = '\n'.join(f'  {i + 1}. {label}' for i, label in enumerate(labels)) + '\n  0. Назад'
        while True:
            selection_screen(page, items)
            answer = action_choice(items, selection_screen, page)
            if answer == '0':
                return []
            if answer.isdecimal() and 1 <= int(answer) <= len(labels):
                return [int(answer) - 1]
            selection_screen(page, items, 'Введите один номер из списка.')

    while True:
        try:
            title, options = 'Главное меню', menu
            show('Выберите действие цифрой. Подключение создаёт ссылки на файлы из пакетов.')
            choice = action_choice(menu, render)
            if choice == '0':
                return
            if choice == '5':
                cleanup = choose('Главное меню → Очистка',
                                 ['Удалить бэкапы — выбор по пакетам или отдельным файлам',
                                  'Удалить старые ссылки'])
                if not cleanup:
                    continue
                choice = 'clean' if cleanup == [0] else 'prune'
            if choice == 'prune':
                stale = repository_links(repo, target, orphan_only=True)
                if not stale:
                    show('Ссылки на удалённые пакеты или файлы не найдены.')
                    wait()
                    continue
                orphan_names = sorted({name for name, _, _ in stale})
                indexes = choose('Главное меню → Очистка → Старые ссылки', orphan_names,
                                 actions=True, verb='Удалить')
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
            if choice == '7':
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
                         'Выберите «Подключить пакеты» в главном меню для подключения.')
                wait()
                continue
            names = sorted(packages_in(repo))
            if choice == '3':
                names = sorted(set(names) | {name for name, _, _ in repository_links(repo, target)})
            if choice not in {'1', '2', '3', '4', '6', 'clean'}:
                show('Введите номер от 0 до 7.')
                wait()
                continue
            if not names:
                show('Пакеты не найдены. Добавьте папку пакета в репозиторий.')
                wait()
                continue
            if choice in {'2', '4'}:
                show(execute(['status' if choice == '2' else 'backups', '--all']))
                wait()
                continue
            if choice == 'clean':
                mode = choose('Главное меню → Очистка → Бэкапы',
                              ['Выбрать пакеты', 'Выбрать отдельные бэкапы'])
                if not mode:
                    continue
                if mode == [0]:
                    connected = connected_packages(repo, target)
                    indexes = choose('Главное меню → Очистка → Бэкапы пакетов', names,
                                     actions=True, verb='Удалить',
                                     connected={i for i, name in enumerate(names) if name in connected})
                    if not indexes:
                        continue
                    command = ['clean'] + [names[index] for index in indexes]
                else:
                    command = None
            else:
                command = None
            if choice == 'clean' and command is None:
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
                                 [name + '-backup' for name in backups], actions=True, verb='Удалить')
                if not indexes:
                    continue
                command = ['clean', '--all']
                for index in indexes:
                    command += ['--file', backups[index]]
            elif choice != 'clean':
                action_title = {'1': 'Подключить пакеты', '3': 'Отключить пакеты',
                                '6': 'Посмотреть, что изменится'}[choice]
                connected = connected_packages(repo, target)
                indexes = choose('Главное меню → ' + action_title, names, actions=True,
                                 verb={'1': 'Подключить', '3': 'Отключить', '6': 'Проверить'}[choice],
                                 connected={i for i, name in enumerate(names) if name in connected})
                if not indexes:
                    continue
                selection = [names[index] for index in indexes]
                action = 'remove' if choice == '3' else 'apply'
                command = [action] + selection
            preview = execute(command + ['--dry-run'])
            show('Будут выполнены действия:\n' + preview)
            if choice == '6':
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
    if any(parent in destinations for dest in destinations for parent in dest.parents):
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
            with operation_lock(repo):
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
            if dest.is_symlink() and not dest.is_file():
                raise ValueError(f'Нельзя сохранить содержимое ссылки в бэкап: {dest}')
            if exists(backup):
                raise ValueError(f'Уже есть бэкап: {backup}. Существующий конфиг нельзя сохранить поверх него. '
                                 'Сохраните бэкап в другом месте или удалите через меню, затем повторите подключение.')
            print(f'Сохранить бэкап: {dest} -> {backup}')
    command = [stow, '--dir', str(repo), '--target', str(target), '--no-folding',
               '--ignore=(^|/)[^/]*-backup($|/)', '--stow' if args.command == 'apply' else '--delete'] + selected
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
    with operation_lock(repo):
        apply_stow(args, repo, entries, conflicts, command)


def apply_stow(args, repo, entries, conflicts, command):
    moved = []
    original = {dest for _, dest, _ in entries if exists(dest)}
    try:
        if args.command == 'apply':
            for dest, backup in conflicts:
                link_target = os.readlink(dest) if dest.is_symlink() else None
                if link_target is None:
                    dest.rename(backup)
                else:
                    # Snapshot the user's file, not the link or package content.
                    with backup.open('xb') as output:
                        try:
                            with dest.open('rb') as source:
                                shutil.copyfileobj(source, output)
                        except OSError:
                            output.close()
                            backup.unlink()
                            raise
                    try:
                        shutil.copystat(dest, backup)
                        dest.unlink()
                    except OSError:
                        backup.unlink()
                        raise
                moved.append((dest, backup, link_target))
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
            sources = {dest: source for source, dest, _ in entries}
            for dest, backup, link_target in reversed(moved):
                source = sources[dest]
                if managed(dest, source):
                    dest.unlink()
                if not exists(dest):
                    if link_target is None:
                        backup.rename(dest)
                    else:
                        dest.symlink_to(link_target)
                        backup.unlink()
        raise


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)
