"""Read-only view models and adapter to the existing dotctl backend."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dotctl


def command(repo, target, arguments):
    result = subprocess.run(
        [sys.executable, str(ROOT / 'dotctl.py'), '--repo', str(repo),
         '--target', str(target), *arguments],
        capture_output=True, text=True, encoding='utf-8',
        env=dict(os.environ, PYTHONIOENCODING='utf-8'),
    )
    output = '\n'.join(part.strip() for part in [result.stdout, result.stderr] if part.strip())
    if result.returncode:
        raise ValueError(output or f'Операция завершилась с кодом {result.returncode}')
    return output


def snapshot(repo, target):
    if not repo.is_dir() or not target.is_dir():
        raise ValueError('Выберите существующие папки репозитория и назначения.')
    packages, backups = [], []
    links = dotctl.repository_links(repo, target)
    stale = {}
    for name, link, source in links:
        if not source.exists():
            stale.setdefault(name, []).append((link, source))
    for name, folder in sorted(dotctl.packages_in(repo).items()):
        linked, total, local = 0, 0, 0
        error = ''
        try:
            if folder.is_symlink():
                raise ValueError('Папка пакета является ссылкой')
            for source in dotctl.files(folder):
                relative = source.relative_to(folder)
                dest = target / relative
                total += 1
                if dotctl.managed(dest, source):
                    linked += 1
                elif dotctl.exists(dest):
                    local += 1
                backup = dest.with_name(dest.name + '-backup')
                if dotctl.exists(backup):
                    backups.append(dict(package=name, relative=str(relative), path=str(backup)))
        except (OSError, ValueError, RuntimeError) as exc:
            error = str(exc)
        state = ('Требует проверки' if error else 'Пустой пакет' if not total else
                 'Подключён' if linked == total else 'Частично' if linked else 'Не подключён')
        packages.append(dict(name=name, state=state, linked=linked, total=total, local=local, error=error))
    return dict(packages=packages, backups=backups, stale=stale)
