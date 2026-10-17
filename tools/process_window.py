"""A disposable viewer; its lifetime never owns a worker or pending journal."""
import tkinter as tk
from tkinter import ttk
from collections import deque


class ProcessWindow:
    def __init__(self, parent, events, open_logs, context='本轮执行回执'):
        self.window = tk.Toplevel(parent)
        self.window.title('棕色尘埃2 · 执行过程')
        self.window.geometry('820x490'); self.window.minsize(640, 350)
        self.window.configure(bg='#171b23')
        self.auto_scroll = tk.BooleanVar(value=True)
        bar = ttk.Frame(self.window, padding=(16, 12)); bar.pack(fill='x')
        info = ttk.Frame(bar); info.pack(side='left', fill='x', expand=True)
        ttk.Label(info, text='执行过程', foreground='#e7d5ae', font=('Microsoft YaHei UI', 13, 'bold')).pack(anchor='w')
        self.context = tk.StringVar(value=context)
        ttk.Label(info, textvariable=self.context, style='Muted.TLabel').pack(anchor='w', pady=(3, 0))
        ttk.Checkbutton(bar, text='自动滚动', variable=self.auto_scroll, command=self.follow).pack(side='right')
        ttk.Button(bar, text='跳到最新', command=self.latest).pack(side='right', padx=10)
        frame = ttk.Frame(self.window, padding=(16, 0, 16, 0)); frame.pack(fill='both', expand=True)
        self.text = tk.Text(frame, width=1, height=12, bg='#1e2430', fg='#eeeae0', insertbackground='#eeeae0', relief='flat',
                            wrap='word', font=('Microsoft YaHei UI', 10), padx=12, pady=10, spacing3=6,
                            highlightthickness=1, highlightbackground='#343e50', state='disabled')
        scroll = ttk.Scrollbar(frame, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y'); self.text.pack(fill='both', expand=True)
        for level, color in {'info': '#c9cfda', 'wait': '#e7ba79', 'warning': '#e7ba79', 'error': '#ef9796', 'success': '#8bd2b0'}.items():
            self.text.tag_configure(level, foreground=color)
        footer = ttk.Frame(self.window, padding=(16, 8)); footer.pack(side='bottom', fill='x', before=frame)
        ttk.Label(footer, text='显示接收时间 · 保留最近5000条\n关闭窗口不停止任务', style='Muted.TLabel').pack(side='left')
        ttk.Button(footer, text='运行记录', command=open_logs).pack(side='right')
        self.count, self.boundaries = 0, deque()
        for event in events:
            self.append(event)

    def append(self, event):
        self.text.mark_set('reading_position', self.text.index('@0,0'))
        self.text.mark_gravity('reading_position', 'left')
        self.text.configure(state='normal')
        mark = f'event_{self.count}'
        self.text.mark_set(mark, 'end-1c'); self.text.mark_gravity(mark, 'left')
        self.boundaries.append(mark)
        self.text.insert('end', event.line(), event.level)
        self.count += 1
        if len(self.boundaries) > 5000:
            old = self.boundaries.popleft()
            self.text.delete('1.0', self.boundaries[0]); self.text.mark_unset(old)
        self.text.configure(state='disabled')
        if self.auto_scroll.get():
            self.text.see('end')
        else:
            self.text.yview('reading_position')

    def follow(self):
        if self.auto_scroll.get():
            self.latest()

    def latest(self):
        self.text.see('end')

    def show(self):
        self.window.deiconify(); self.window.lift(); self.window.focus_force()
