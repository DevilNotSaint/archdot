"""Small Canvas widgets: rounded surfaces and short, cancellable animations."""
import tkinter as tk
from tkinter import font


def blend(start, end, amount):
    values = [round(int(start[i:i + 2], 16) * (1 - amount) + int(end[i:i + 2], 16) * amount)
              for i in (1, 3, 5)]
    return '#' + ''.join(f'{value:02x}' for value in values)


def rounded(canvas, bounds, radius=14, **options):
    x1, y1, x2, y2 = bounds
    r = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
    return canvas.create_polygon(
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        smooth=True, splinesteps=24, **options)


class Surface(tk.Frame):
    def __init__(self, parent, color, padding=14, **kwargs):
        super().__init__(parent, bg=parent.cget('bg'), **kwargs)
        self.canvas = tk.Canvas(self, bg=self.cget('bg'), bd=0, highlightthickness=0)
        self.canvas.place(relwidth=1, relheight=1)
        self.body = tk.Frame(self, bg=color)
        self.body.pack(fill='both', expand=True, padx=padding, pady=padding)
        self.bind('<Configure>', lambda event: self.paint(event.width, event.height, color))

    def paint(self, width, height, color):
        self.canvas.delete('all')
        rounded(self.canvas, (0, 0, width, height), fill=color, outline='')


class SoftButton(tk.Canvas):
    def __init__(self, parent, text, command, fill, foreground, primary=False):
        self.text, self.command = text, command
        self.fill, self.foreground = fill, foreground
        self.primary = primary
        self.state, self.over, self.pressed, self.focused = 'normal', False, False, False
        self.job = None
        self.current = fill
        self.face = font.Font(family='DejaVu Sans', size=10)
        super().__init__(parent, width=self.face.measure(text) + 32, height=42,
                         bg=parent.cget('bg'), bd=0, highlightthickness=0,
                         cursor='hand2', takefocus=True)
        self.bind('<Configure>', lambda _event: self.paint())
        self.bind('<Enter>', lambda _event: self.hover(True))
        self.bind('<Leave>', lambda _event: self.hover(False))
        self.bind('<FocusIn>', lambda _event: self.focus(True))
        self.bind('<FocusOut>', lambda _event: self.focus(False))
        self.bind('<ButtonPress-1>', self.press)
        self.bind('<ButtonRelease-1>', self.release)
        self.bind('<Return>', lambda _event: self.invoke())
        self.bind('<space>', lambda _event: self.invoke())
        self.paint()

    def configure(self, cnf=None, **kwargs):
        if cnf:
            kwargs.update(cnf)
        redraw = False
        for key, attribute in [('bg', 'fill'), ('fg', 'foreground'), ('state', 'state'), ('text', 'text')]:
            if key in kwargs:
                setattr(self, attribute, kwargs.pop(key))
                redraw = True
        result = super().configure(**kwargs) if kwargs else None
        if redraw:
            self.transition()
        return result

    config = configure

    def focus(self, value):
        self.focused = value
        self.paint()

    def hover(self, value):
        self.over = value
        if not value:
            self.pressed = False
        self.transition()

    def press(self, _event):
        if self.state != 'disabled':
            self.focus_set()
            self.pressed = True
            self.paint()

    def release(self, event):
        activate = self.pressed and 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        self.pressed = False
        self.paint()
        if activate:
            self.invoke()

    def invoke(self):
        if self.state != 'disabled':
            self.command()

    def transition(self):
        if self.job:
            self.after_cancel(self.job)
            self.job = None
        target = blend(self.fill, '#ddd2f3', 0.13) if self.over and self.state != 'disabled' else self.fill
        if self.state == 'disabled':
            target = blend(self.fill, '#211e2b', 0.55)
        start = self.current
        frames = 9 if getattr(self._root(), 'animations', True) else 1
        def tick(step=1):
            self.job = None
            t = 1 - (1 - step / frames) ** 3
            self.current = blend(start, target, t)
            self.paint()
            if step < frames:
                self.job = self.after(16, lambda: tick(step + 1))
        tick()

    def paint(self):
        self.delete('all')
        width = max(self.winfo_width(), int(self.cget('width')) if self.winfo_width() <= 1 else 0)
        height = max(self.winfo_height(), 42)
        rounded(self, (2, 2, width - 2, height - 2), radius=13, fill=self.current,
                outline='#bcabdb' if self.focused and self.state != 'disabled' else '', width=1)
        self.create_text(width / 2, height / 2 + int(self.pressed), text=self.text, font=self.face,
                         fill='#8f889e' if self.state == 'disabled' else self.foreground)

    def destroy(self):
        if self.job:
            self.after_cancel(self.job)
            self.job = None
        super().destroy()
