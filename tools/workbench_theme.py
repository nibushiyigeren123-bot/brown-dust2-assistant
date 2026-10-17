"""Decorative native widgets. This module never imports execution or game controls."""
import tkinter as tk
from PIL import Image, ImageTk
from project_paths import ROOT


class BrandBanner(tk.Canvas):
    def __init__(self, parent, context, settings):
        super().__init__(parent, height=114, bg='#171b23', highlightthickness=0)
        self.source = Image.open(ROOT / 'assets/workbench/comfy_frame.png').convert('RGB')
        self.context, self.settings = context, settings
        self.button = tk.Button(self, text='日常设置', command=settings, bg='#283140', fg='#eeeae0',
                                activebackground='#3a4960', activeforeground='#ffffff',
                                font=('Microsoft YaHei UI', 10), relief='flat', padx=20, pady=8, cursor='hand2')
        self.bind('<Configure>', self.paint)
        context.trace_add('write', lambda *_: self.paint())

    def paint(self, event=None):
        width = max(600, self.winfo_width())
        self.picture = ImageTk.PhotoImage(self.source.resize((width, 114), Image.Resampling.LANCZOS))
        self.delete('all')
        self.create_image(0, 0, image=self.picture, anchor='nw')
        self.create_text(74, 27, text='BROWN DUST Ⅱ  /  DAILY OPERATIONS', anchor='w', fill='#e7d5ae', font=('Georgia', 10))
        self.create_text(74, 59, text='日常作战台', anchor='w', fill='#f5efe2', font=('Microsoft YaHei UI', 23, 'bold'))
        self.create_text(74, 89, text=self.context.get(), anchor='w', fill='#b3c8e4', font=('Microsoft YaHei UI', 10))
        self.create_text(width - 230, 52, text='原阵容保持  ·  邮箱手动', anchor='e', fill='#e7d5ae', font=('Microsoft YaHei UI', 10))
        self.create_window(width - 130, 55, window=self.button, width=120, height=38)
