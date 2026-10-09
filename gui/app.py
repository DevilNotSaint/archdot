#!/usr/bin/env python3
"""Lightweight native dark interface for dotctl. No third-party dependencies."""
import argparse
from pathlib import Path
import queue
import threading
try:
    import tkinter as tk
    from tkinter import filedialog, ttk
except ImportError as exc:
    raise SystemExit('Для графического интерфейса нужен Tkinter.\n'
                     'Ubuntu/Debian: sudo apt install python3-tk\n'
                     'Arch: sudo pacman -S tk\n'
                     f'Подробности: {exc}') from None

try:
    from . import model
    from .widgets import Surface, SoftButton, blend
except ImportError:
    import model
    from widgets import Surface, SoftButton, blend

BG = '#191720'
PANEL = '#211e2b'
CARD = '#2b2737'
LINE = '#3a3449'
TEXT = '#ede8f4'
MUTED = '#ada4bd'
PURPLE = '#b8a4df'


class App(tk.Tk):
    def __init__(self, repo, target, animations=True):
        super().__init__()
        self.title('Dotfiles · ваш дом для конфигов')
        self.geometry('1120x780')
        self.minsize(900, 650)
        self.configure(bg=BG)
        self.repo, self.target = repo, target
        self.data = dict(packages=[], backups=[], stale={})
        self.page = 'packages'
        self.busy = False
        self.events = queue.Queue()
        self.controls = []
        self.rows = {}
        self.animations = animations
        self.transition_job = None
        self.hover_row = ''
        self.pulse_job = None
        self.setup_style()
        self.build()
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.after(60, self.poll)
        self.refresh()

    def setup_style(self):
        style = ttk.Style(self)
        style.theme_use('clam')
        self.option_add('*Font', ('DejaVu Sans', 10))
        style.configure('Treeview', background=PANEL, fieldbackground=PANEL,
                        foreground=TEXT, borderwidth=0, rowheight=46, font=('DejaVu Sans', 10))
        style.configure('Treeview.Heading', background=CARD, foreground=MUTED,
                        relief='flat', padding=(12, 10), font=('DejaVu Sans', 9))
        style.map('Treeview', background=[('selected', '#443952')], foreground=[('selected', '#f1e9ff')])
        style.map('Treeview.Heading', background=[('active', LINE)])
        style.configure('Vertical.TScrollbar', background=LINE, troughcolor=PANEL,
                        arrowcolor=MUTED, borderwidth=0)

    def label(self, parent, text, size=10, color=TEXT, bg=None, **kwargs):
        return tk.Label(parent, text=text, bg=bg or parent.cget('bg'), fg=color,
                        font=('DejaVu Sans', size), **kwargs)

    def button(self, parent, text, action, primary=False):
        button = SoftButton(parent, text, action, PURPLE if primary else CARD,
                            BG if primary else TEXT, primary)
        self.controls.append(button)
        return button

    def build(self):
        sidebar = tk.Frame(self, bg=PANEL, width=210)
        sidebar.pack(side='left', fill='y')
        sidebar.pack_propagate(False)
        self.label(sidebar, '◈  dotfiles', 22, PURPLE).pack(anchor='w', padx=22, pady=(30, 5))
        self.label(sidebar, 'Конфиги на своих местах', 9, MUTED).pack(anchor='w', padx=22, pady=(0, 35))
        self.nav = {}
        for key, title in [('packages', 'Пакеты'), ('backups', 'Бэкапы'), ('stale', 'Старые ссылки')]:
            button = self.button(sidebar, title, lambda key=key: self.navigate(key))
            button.pack(fill='x', padx=14, pady=4)
            self.nav[key] = button
        self.label(sidebar, 'Всё на своих местах\nВаши конфиги, ваш порядок', 9, MUTED,
                   justify='left').pack(side='bottom', anchor='w', padx=22, pady=24)

        main = tk.Frame(self, bg=BG)
        main.pack(side='left', fill='both', expand=True, padx=28, pady=25)
        header = tk.Frame(main, bg=BG)
        header.pack(fill='x')
        self.heading = self.label(header, 'Ваши пакеты', 24)
        self.heading.pack(side='left')
        self.button(header, '+ Добавить пакет', self.add_package, True).pack(side='right')
        self.subtitle = self.label(main, '', 10, MUTED, anchor='w', justify='left', wraplength=750)
        self.subtitle.pack(fill='x', pady=(8, 20))

        location_card = Surface(main, PANEL)
        location_card.pack(fill='x')
        locations = location_card.body
        self.repo_label = self.label(locations, '', 9, MUTED, anchor='w')
        self.repo_label.pack(fill='x')
        self.target_label = self.label(locations, '', 9, MUTED, anchor='w')
        self.target_label.pack(fill='x', pady=(6, 0))
        path_actions = tk.Frame(locations, bg=PANEL)
        path_actions.pack(fill='x', pady=(10, 0))
        self.button(path_actions, 'Папка dotfiles…', lambda: self.change_path('repo')).pack(side='left')
        self.button(path_actions, 'Куда подключать…', lambda: self.change_path('target')).pack(side='left', padx=8)
        self.button(path_actions, 'Обновить', self.refresh).pack(side='right')

        metrics = tk.Frame(main, bg=BG)
        metrics.pack(fill='x', pady=(16, 16))
        self.metrics = []
        for index, caption in enumerate(['Пакеты', 'Резервные копии', 'Старые ссылки']):
            metrics.columnconfigure(index, weight=1, uniform='metric')
            card = Surface(metrics, PANEL, padding=10)
            card.grid(row=0, column=index, sticky='ew', padx=(0, 10 if index < 2 else 0))
            value = self.label(card.body, '—', 20, PURPLE)
            value.pack(anchor='w')
            self.label(card.body, caption, 9, MUTED).pack(anchor='w', pady=(2, 0))
            self.metrics.append(value)
        self.search = tk.StringVar()
        self.search.trace_add('write', lambda *_: self.render_rows())
        search_card = Surface(main, CARD, padding=8)
        search_card.pack(fill='x')
        self.label(search_card.body, '⌕', 16, MUTED).pack(side='left', padx=(5, 10))
        entry = tk.Entry(search_card.body, textvariable=self.search, bg=CARD, fg=TEXT,
                         insertbackground=PURPLE, relief='flat', bd=0, highlightthickness=0)
        entry.pack(side='left', fill='x', expand=True)
        self.label(main, 'Поиск по имени или пути · Ctrl / Shift для выбора нескольких строк', 8, MUTED,
                   anchor='w').pack(fill='x', pady=(5, 10))
        table_card = Surface(main, PANEL, padding=8)
<<<<<<< HEAD
=======
        table_card.pack(fill='both', expand=True)
>>>>>>> 35f7c3512f34e187b3f593f86ca683801d7ffb07
        table = table_card.body
        self.tree = ttk.Treeview(table, columns=('name', 'state', 'detail'), show='headings', selectmode='extended')
        self.tree.pack(side='left', fill='both', expand=True)
        scrollbar = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview)
        scrollbar.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind('<<TreeviewSelect>>', self.describe_selection)
        self.tree.bind('<Motion>', self.highlight_row)
        self.tree.bind('<Leave>', lambda _event: self.highlight_row())
        self.tree.tag_configure('stripe', background='#25212f')
        self.tree.tag_configure('hover', background='#322c40')
        self.tree.tag_configure('connected', foreground='#b3d8ba')
        self.tree.tag_configure('attention', foreground='#dbbf98')
        self.actions = tk.Frame(main, bg=BG)
        self.actions.pack(fill='x', pady=(14, 10))
        self.details = self.label(main, '', 9, MUTED, anchor='w', justify='left', wraplength=780)
        self.details.pack(fill='x', pady=(0, 10))
        status_bar = tk.Frame(main, bg=BG)
        status_bar.pack(fill='x')
        self.activity = tk.Canvas(status_bar, width=16, height=18, bg=BG, bd=0, highlightthickness=0)
        self.activity.pack(side='left', padx=(0, 7))
        self.activity_dot = self.activity.create_oval(4, 5, 11, 12, fill=PURPLE, outline='')
        self.status = self.label(status_bar, 'Загрузка…', 9, MUTED, anchor='w')
        self.status.pack(side='left', fill='x', expand=True)
<<<<<<< HEAD
        # Reserve space for actions before giving the table the remaining height.
        status_bar.pack_configure(side='bottom', before=self.actions)
        self.details.pack_configure(side='bottom', before=self.actions)
        self.actions.pack_configure(side='bottom')
        table_card.pack(fill='both', expand=True)
=======
>>>>>>> 35f7c3512f34e187b3f593f86ca683801d7ffb07
        self.render_page()

    def animate_page(self):
        if self.transition_job:
            self.after_cancel(self.transition_job)
            self.transition_job = None
        frames = 12 if self.animations else 1
        def tick(step=1):
            self.transition_job = None
            self.heading.config(fg=blend(MUTED, TEXT, step / frames))
            if step < frames:
                self.transition_job = self.after(16, lambda: tick(step + 1))
        tick()

    def highlight_row(self, event=None):
        row = self.tree.identify_row(event.y) if event else ''
        if row == self.hover_row:
            return
        if self.hover_row and self.tree.exists(self.hover_row):
            tags = tuple(tag for tag in self.tree.item(self.hover_row, 'tags') if tag != 'hover')
            self.tree.item(self.hover_row, tags=tags)
        self.hover_row = row
        if row:
            self.tree.item(row, tags=('hover',) + tuple(self.tree.item(row, 'tags')))

    def pulse(self, step=0):
        if self.pulse_job:
            self.after_cancel(self.pulse_job)
            self.pulse_job = None
        if not self.busy or not self.animations:
            self.activity.itemconfig(self.activity_dot, fill=PURPLE if self.busy else '#8bb6a0')
            return
        amount = abs((step % 40) - 20) / 20
        self.activity.itemconfig(self.activity_dot, fill=blend('#51445f', PURPLE, amount))
        self.pulse_job = self.after(40, lambda: self.pulse(step + 1))

    def navigate(self, page):
        if self.busy:
            return
        self.page = page
        self.search.set('')
        self.render_page()
        self.animate_page()

    def render_page(self):
        titles = {'packages': ('Ваши пакеты', 'Подключайте конфиги. Старые файлы сохраняются рядом с суффиксом -backup.'),
                  'backups': ('Резервные копии', 'Копии конфигов для существующих пакетов. Удаление необратимо.'),
                  'stale': ('Старые ссылки', 'Ссылки на удалённые пакеты и файлы. Рабочие ссылки сохраняются.')}
        title, subtitle = titles[self.page]
        self.heading.config(text=title)
        self.subtitle.config(text=subtitle)
        for key, button in self.nav.items():
            button.config(bg='#393044' if key == self.page else CARD,
                          fg=PURPLE if key == self.page else TEXT)
        for child in self.actions.winfo_children():
            if child in self.controls:
                self.controls.remove(child)
            child.destroy()
        if self.page == 'packages':
            actions = [('Подключить выбранные', lambda: self.operate('apply'), True),
                       ('Отключить', lambda: self.operate('remove'), False),
                       ('Подключить всё', lambda: self.operate('apply', True), False)]
        elif self.page == 'backups':
            actions = [('Удалить выбранные', lambda: self.operate('clean'), False),
                       ('Удалить все бэкапы', lambda: self.operate('clean', True), False)]
        else:
            actions = [('Убрать выбранные', lambda: self.operate('prune'), True),
                       ('Убрать все старые ссылки', lambda: self.operate('prune', True), False)]
        for text, action, primary in actions:
            self.button(self.actions, text, action, primary).pack(side='left', padx=(0, 8))
        self.render_rows()

    def render_rows(self):
        self.tree.delete(*self.tree.get_children())
        self.hover_row = ''
        self.rows = {}
        query = self.search.get().casefold()
        headers = {'packages': ('Пакет', 'Состояние', 'Конфиги'),
                   'backups': ('Конфиг', 'Пакет', 'Резервная копия'),
                   'stale': ('Пакет', 'Старые ссылки', 'Пример пути')}
        for column, title, width in zip(('name', 'state', 'detail'), headers[self.page], (180, 170, 320)):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, minwidth=100, anchor='w')
        records = self.data[self.page]
        if self.page == 'stale':
            records = [dict(name=name, links=links) for name, links in sorted(records.items())]
        for record in records:
            if self.page == 'packages':
                values = (record['name'], record['state'], f"{record['linked']} / {record['total']} подключено")
                tag = 'connected' if record['state'] == 'Подключён' else 'attention' if record['error'] else ''
            elif self.page == 'backups':
                values = (record['relative'], record['package'], record['path'])
                tag = ''
            else:
                values = (record['name'], str(len(record['links'])), str(record['links'][0][0]))
                tag = 'attention'
            if query and query not in ' '.join(values).casefold():
                continue
            tags = (tag,) + (('stripe',) if len(self.rows) % 2 else ())
            item = self.tree.insert('', 'end', values=values, tags=tags)
            self.rows[item] = record
        self.repo_label.config(text=f'DOTFILES   {self.repo}')
        self.target_label.config(text=f'НАЗНАЧЕНИЕ   {self.target}')
        for label, count in zip(self.metrics, (len(self.data['packages']), len(self.data['backups']),
                                               sum(len(v) for v in self.data['stale'].values()))):
            label.config(text=str(count))
        self.details.config(text='Выберите строки для действия.' if self.rows else
                            'Здесь пока пусто. Добавьте пакет или выберите другую папку dotfiles.' if self.page == 'packages' else
                            'Ничего не найдено — в этом разделе нет подходящих элементов.')

    def describe_selection(self, _event=None):
        selected = [self.rows[item] for item in self.tree.selection() if item in self.rows]
        if not selected:
            return
        if self.page == 'packages' and len(selected) == 1:
            record = selected[0]
            text = record['error'] or f"{record['name']}: {record['local']} локальных конфигов будут сохранены при подключении."
        elif self.page == 'backups' and len(selected) == 1:
            text = selected[0]['path']
        else:
            text = f'Выбрано: {len(selected)}'
        self.details.config(text=text)

    def task(self, title, work, done):
        if self.busy:
            return
        self.busy = True
        self.pulse()
        for control in self.controls:
            control.config(state='disabled')
        self.status.config(text=title)
        def worker():
            try:
                self.events.put((done, work(), None))
            except Exception as exc:
                self.events.put((done, None, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            done, result, error = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.pulse()
            for control in self.controls:
                control.config(state='normal')
            if error:
                self.status.config(text='Не удалось выполнить действие. Подробности в сообщении.')
                self.dialog('Нужна проверка', error)
            else:
                self.status.config(text='Готово')
                done(result)
        self.after(60, self.poll)

    def refresh(self):
        self.task('Проверяем конфиги…', lambda: model.snapshot(self.repo, self.target), self.loaded)

    def loaded(self, data):
        self.data = data
        self.render_rows()

    def change_path(self, which):
        if self.busy:
            return
        selected = filedialog.askdirectory(parent=self, title='Папка dotfiles' if which == 'repo' else 'Куда подключать конфиги',
                                           initialdir=str(self.repo if which == 'repo' else self.target))
        if selected:
            setattr(self, which, Path(selected).resolve())
            self.data = dict(packages=[], backups=[], stale={})
            self.render_rows()
            self.refresh()

    def operate(self, action, all_items=False):
        if self.busy:
            return
        records = [self.rows[item] for item in self.tree.selection() if item in self.rows]
        if not all_items and not records:
            self.status.config(text='Сначала выберите одну или несколько строк в списке.')
            return
        args = [action, '--all'] if all_items else [action]
        if not all_items:
            if action == 'clean':
                args += sorted({record['package'] for record in records})
                for relative in sorted({record['relative'] for record in records}):
                    args += ['--file', relative]
            else:
                args += [record['name'] for record in records]
        def preview_ready(output):
            title = {'apply': 'Подключить конфиги?', 'remove': 'Отключить конфиги?',
                     'clean': 'Удалить резервные копии?', 'prune': 'Убрать старые ссылки?'}[action]
            note = 'Удаление резервных копий необратимо.\n\n' if action == 'clean' else ''
            self.dialog(title, note + output, lambda: self.perform(args), 'Удалить' if action == 'clean' else 'Выполнить')
        self.task('Проверяем изменения…', lambda: model.command(self.repo, self.target, args + ['--dry-run']), preview_ready)

    def perform(self, args):
        self.task('Выполняем действие…', lambda: model.command(self.repo, self.target, args + ['--yes']), self.completed)

    def completed(self, output):
        self.dialog('Готово', output or 'Действие выполнено.')
        self.refresh()

<<<<<<< HEAD
    def place_dialog(self, window):
        window.update_idletasks()
        width = window.winfo_reqwidth()
        height = window.winfo_reqheight()
        window.minsize(width, height)
        self.update_idletasks()
        x = max(0, min(self.winfo_rootx() + (self.winfo_width() - width) // 2,
                       window.winfo_screenwidth() - width))
        y = max(0, min(self.winfo_rooty() + (self.winfo_height() - height) // 2,
                       window.winfo_screenheight() - height))
        window.geometry(f'{width}x{height}+{x}+{y}')
        window.deiconify()
        window.grab_set()

=======
>>>>>>> 35f7c3512f34e187b3f593f86ca683801d7ffb07
    def dialog(self, title, text, on_confirm=None, confirm_text='Выполнить'):
        window = tk.Toplevel(self)
        window.title(title)
        window.configure(bg=PANEL)
        window.geometry('740x490')
        window.minsize(560, 340)
        window.transient(self)
        window.grab_set()
        self.label(window, title, 18).pack(anchor='w', padx=24, pady=(22, 14))
        box = tk.Frame(window, bg=PANEL)
        box.pack(fill='both', expand=True, padx=24)
        content = tk.Text(box, bg=BG, fg=TEXT, wrap='word', relief='flat', bd=12,
                          font=('DejaVu Sans', 10), insertbackground=PURPLE)
        content.insert('1.0', text)
        content.config(state='disabled')
        content.pack(side='left', fill='both', expand=True)
        scroll = ttk.Scrollbar(box, command=content.yview)
        scroll.pack(side='right', fill='y')
        content.config(yscrollcommand=scroll.set)
        footer = tk.Frame(window, bg=PANEL)
        footer.pack(fill='x', padx=24, pady=20)
        # Dialog buttons are short-lived and excluded from the global busy list.
        def button(text, command, primary=False):
            result = self.button(footer, text, command, primary)
            self.controls.remove(result)
            result.pack(side='right', padx=(8, 0))
            return result
        if on_confirm:
            def accept():
                window.destroy()
                on_confirm()
            button(confirm_text, accept, True)
        close = button('Отмена' if on_confirm else 'Закрыть', window.destroy)
        close.focus_set()
        window.bind('<Escape>', lambda _event: window.destroy())

    def add_package(self):
        if self.busy:
            return
        window = tk.Toplevel(self)
<<<<<<< HEAD
        window.withdraw()
        window.title('Добавить пакет')
        window.configure(bg=PANEL)
        window.transient(self)
=======
        window.title('Добавить пакет')
        window.configure(bg=PANEL)
        window.geometry('640x350')
        window.transient(self)
        window.grab_set()
>>>>>>> 35f7c3512f34e187b3f593f86ca683801d7ffb07
        self.label(window, 'Новый дом для конфига', 18).pack(anchor='w', padx=24, pady=(24, 10))
        self.label(window, 'Выберите файл или папку. Вложенная структура сохранится.', 10, MUTED).pack(anchor='w', padx=24)
        form = tk.Frame(window, bg=PANEL)
        form.pack(fill='x', padx=24, pady=18)
        self.label(form, 'Имя пакета').grid(row=0, column=0, sticky='w', pady=8)
        name = tk.StringVar()
        tk.Entry(form, textvariable=name, bg=CARD, fg=TEXT, insertbackground=PURPLE, relief='flat', bd=8).grid(row=0, column=1, sticky='ew', padx=(12, 0))
        path = tk.StringVar()
        self.label(form, 'Источник').grid(row=1, column=0, sticky='w', pady=8)
        tk.Entry(form, textvariable=path, bg=CARD, fg=TEXT, insertbackground=PURPLE, relief='flat', bd=8).grid(row=1, column=1, sticky='ew', padx=(12, 0))
        form.columnconfigure(1, weight=1)
        hint = self.label(window, '', 9, MUTED)
        hint.pack(anchor='w', padx=24)
        actions = tk.Frame(window, bg=PANEL)
<<<<<<< HEAD
        actions.pack(fill='x', padx=24, pady=(10, 24))
=======
        actions.pack(fill='x', padx=24)
>>>>>>> 35f7c3512f34e187b3f593f86ca683801d7ffb07
        def pick(folder):
            chooser = filedialog.askdirectory if folder else filedialog.askopenfilename
            selected = chooser(parent=window, initialdir=str(self.target), title='Выберите конфиги')
            if selected:
                path.set(selected)
                if not name.get():
                    candidate = Path(selected).name if folder else Path(selected).stem.lstrip('.')
                    name.set(candidate)
        def local_button(text, action, primary=False):
            result = self.button(actions, text, action, primary)
            self.controls.remove(result)
            result.pack(side='left', padx=(0, 8))
        local_button('Выбрать файл…', lambda: pick(False))
        local_button('Выбрать папку…', lambda: pick(True))
        def prepare():
            package_name, source_path = name.get().strip(), path.get().strip()
            if not package_name or not source_path:
                hint.config(text='Укажите имя пакета и выберите файл или папку.')
                return
            window.destroy()
            def planned(result):
                _, _, candidates = result
                structure = '\n'.join(f'{package_name}/{item.relative_to(self.target)}' for item in candidates)
                def create():
                    self.task('Создаём пакет…', lambda: model.dotctl.import_package(self.repo, self.target, package_name, source_path),
                              lambda count: self.completed(f'Пакет {package_name} создан: {count} файлов.\nТеперь выберите его и нажмите «Подключить выбранные».'))
                self.dialog('Создать пакет?', 'Исходные файлы останутся на месте.\n\n' + structure, create, 'Создать')
            self.task('Проверяем структуру…', lambda: model.dotctl.package_import_plan(self.repo, self.target, package_name, source_path), planned)
        local_button('Продолжить', prepare, True)
        window.bind('<Escape>', lambda _event: window.destroy())
<<<<<<< HEAD
        self.place_dialog(window)
=======
>>>>>>> 35f7c3512f34e187b3f593f86ca683801d7ffb07

    def close(self):
        if self.busy:
            self.status.config(text='Дождитесь завершения операции перед закрытием приложения.')
            return
        self.destroy()

    def destroy(self):
        for job in (self.transition_job, self.pulse_job):
            if job:
                self.after_cancel(job)
        super().destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=model.ROOT)
    parser.add_argument('--target', type=Path, default=Path.home())
    parser.add_argument('--no-animations', action='store_true', help='Disable motion effects')
    args = parser.parse_args()
    App(args.repo.expanduser().resolve(), args.target.expanduser().resolve(), not args.no_animations).mainloop()


if __name__ == '__main__':
    main()
