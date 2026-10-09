"""Self-closing UI check. Requires a display; uses only temporary config paths."""
from pathlib import Path
import tempfile
import time

from app import App
from widgets import SoftButton


def main():
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        repo, target = base / 'repo', base / 'home'
        (repo / 'bash').mkdir(parents=True)
        (repo / 'bash' / '.bashrc').write_text('new')
        target.mkdir()
        (target / '.bashrc-backup').write_text('old')
        app = App(repo, target)
        app.withdraw()
        callback_errors = []
        app.report_callback_exception = lambda *error: callback_errors.append(error)
        def settle():
            deadline = time.monotonic() + 10
            while app.busy and time.monotonic() < deadline:
                app.update()
                time.sleep(0.01)
            assert not app.busy, 'Background load did not finish'
        try:
            settle()
<<<<<<< HEAD
            app.deiconify()
            app.geometry('900x650')
            app.update()
            for control in app.actions.winfo_children():
                assert control.winfo_ismapped(), 'Package action is hidden'
                assert control.winfo_rooty() + control.winfo_height() <= app.winfo_rooty() + app.winfo_height()
=======
>>>>>>> 35f7c3512f34e187b3f593f86ca683801d7ffb07
            assert len(app.tree.get_children()) == 1
            app.navigate('backups')
            assert len(app.tree.get_children()) == 1
            app.search.set('does-not-exist')
            assert not app.tree.get_children()
            app.navigate('stale')
            assert not app.tree.get_children()
            app.navigate('packages')
            app.dialog('Проверка', 'Тест предварительного просмотра', lambda: None)
            app.update()
            for child in app.winfo_children():
                if child.winfo_class() == 'Toplevel':
                    child.destroy()
            app.add_package()
            app.update()
            for child in app.winfo_children():
                if child.winfo_class() == 'Toplevel':
<<<<<<< HEAD
                    def check_bounds(parent):
                        for widget in parent.winfo_children():
                            if isinstance(widget, SoftButton):
                                assert widget.winfo_rootx() + widget.winfo_width() <= child.winfo_rootx() + child.winfo_width()
                                assert widget.winfo_rooty() + widget.winfo_height() <= child.winfo_rooty() + child.winfo_height()
                            check_bounds(widget)
                    check_bounds(child)
=======
>>>>>>> 35f7c3512f34e187b3f593f86ca683801d7ffb07
                    child.destroy()
            # Exercise the actual asynchronous CLI adapter on an isolated backup.
            original_dialog = app.dialog
            def accept_preview(_title, _text, on_confirm=None, confirm_text='Выполнить'):
                if on_confirm:
                    on_confirm()
            app.dialog = accept_preview
            app.navigate('backups')
            app.tree.selection_set(app.tree.get_children()[0])
            app.operate('clean')
            settle()
            assert not (target / '.bashrc-backup').exists()
            assert not app.tree.get_children()
            app.dialog = original_dialog
            # Disabled controls must not execute actions; rapid hover changes
            # must cancel previous animation and settle on the current state.
            calls = []
            button = SoftButton(app, 'Проверка', lambda: calls.append(True), '#2b2737', '#ede8f4')
            button.config(state='disabled')
            button.invoke()
            assert not calls
            button.config(state='normal')
            button.invoke()
            assert calls == [True]
            button.hover(True)
            button.hover(False)
            app.navigate('packages')
            app.navigate('backups')
            deadline = time.monotonic() + 0.4
            while time.monotonic() < deadline:
                app.update()
                time.sleep(0.01)
            assert button.current == button.fill
            assert button.job is None
            assert app.transition_job is None
            assert app.pulse_job is None
            app.animations = False
            button.hover(True)
            assert button.job is None
            button.destroy()
            assert not callback_errors, callback_errors
            print('GUI smoke check passed')
        finally:
            app.destroy()


if __name__ == '__main__':
    main()
