"""Native workbench presentation; all execution is delegated to isolated workers."""
import json
import os
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, date
from pathlib import Path
from local_cache import cache_path, load_local_config
from collections import deque

from execution_log import LogEvent, ReceiptLog
from process_window import ProcessWindow
from task_progress import ProgressReader, progress_text
from cache_cleanup import automatic_cleanup_enabled, set_automatic_cleanup, clear_old_cache, cleanup_summary

from module_catalog import MODULES, BY_ID, default_plan, validate_plan
from project_paths import ROOT
from game_calendar import game_day
from date_rollover import synchronize_date, require_current_cache

BG, PANEL, EDGE = '#171b23', '#222834', '#343e50'
TEXT, MUTED, BLUE, GOLD = '#eeeae0', '#a6adbb', '#8dafff', '#e7d5ae'
GREEN, AMBER, RED = '#8bd2b0', '#e7ba79', '#ef9796'


class WorkbenchApp:
    def __init__(self, root):
        self.root, self.process, self.output = root, None, None
        from app_icon import set_window_icon
        set_window_icon(root)
        self.stop_file = self.pause_file = None
        self.running_task_id = None
        self.settings_window = self.settings_editor = self.process_view = None
        self.events, self.receipt_log = deque(maxlen=5000), None
        self.log_context = '就绪 · 等待任务'
        self.closing, self.last_log, self.picture = False, '', None
        self.row_status = {}
        self.task_reports, self.task_progress = {}, {}
        self.progress_reader = ProgressReader(ROOT)
        self.completion = tk.StringVar()
        self.auto_cleanup = tk.BooleanVar(value=automatic_cleanup_enabled(ROOT))
        self.cleanup_day = None
        self.queue_summary = tk.StringVar()
        root.title('棕色尘埃2辅助 · 日常作战台 0.2.15')
        root.geometry('1360x900'); root.minsize(1150, 800); root.configure(bg=BG)
        style = ttk.Style(root); style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10), background=BG, foreground=TEXT)
        style.configure('TFrame', background=BG)
        style.configure('Panel.TFrame', background=PANEL)
        style.configure('TLabel', background=BG, foreground=TEXT)
        style.configure('Muted.TLabel', foreground=MUTED)
        style.configure('Panel.TLabel', background=PANEL)
        style.configure('PanelMuted.TLabel', background=PANEL, foreground=MUTED)
        style.configure('TButton', padding=(13, 9), background='#303949', foreground=TEXT, borderwidth=1)
        style.map('TButton', background=[('active', '#44536c'), ('disabled', '#252b35')], foreground=[('disabled', '#707988')])
        style.configure('Primary.TButton', background=GOLD, foreground='#252626', font=('Microsoft YaHei UI', 10, 'bold'))
        style.map('Primary.TButton', background=[('active', '#f4e5c5'), ('disabled', '#454441')], foreground=[('disabled', '#8f8d86')])
        style.configure('Stop.TButton', background='#633b42', foreground='#fff0ee')
        style.configure('TRadiobutton', background=BG, foreground=TEXT, padding=5)
        style.map('TRadiobutton', background=[('active', BG)], foreground=[('disabled', MUTED)])
        style.configure('TCheckbutton', background=BG, foreground=TEXT, padding=4)
        style.map('TCheckbutton', background=[('active', BG)], foreground=[('disabled', MUTED)])
        style.configure('Treeview', background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=38, borderwidth=0)
        style.configure('Treeview.Heading', background='#293140', foreground=MUTED, padding=(5, 10), relief='flat')
        style.map('Treeview', background=[('selected', '#354866')], foreground=[('selected', '#ffffff')])
        style.configure('Horizontal.TProgressbar', troughcolor='#293140', background=BLUE, borderwidth=0)
        style.configure('TCombobox', fieldbackground='#303949', foreground=TEXT, background='#303949', arrowcolor=GOLD)
        style.map('TCombobox', fieldbackground=[('readonly', '#303949'), ('disabled', '#252b35')], foreground=[('readonly', TEXT), ('disabled', MUTED)], selectbackground=[('readonly', '#303949')], selectforeground=[('readonly', TEXT)])
        root.option_add('*TCombobox*Listbox.background', PANEL)
        root.option_add('*TCombobox*Listbox.foreground', TEXT)
        self.execute = tk.BooleanVar(value=False)
        from runtime_policy import network_wait_seconds
        self.wait_choices = {'标准 · 60秒': 60, '耐心 · 120秒': 120, '慢速 · 300秒': 300}
        current_wait = network_wait_seconds()
        self.network_wait = tk.StringVar(value=next((name for name, seconds in self.wait_choices.items() if seconds == current_wait), f'自定义 · {current_wait:g}秒'))
        self.status = tk.StringVar(value='就绪 · 先核对配置，再选择识别或执行')
        self.context = tk.StringVar()
        self.title = tk.StringVar(value='02  当前步骤')
        self.description = tk.StringVar()
        self.scope = tk.StringVar()
        self.mode_label = tk.StringVar(value='只读模式 · 不发送游戏输入')
        self.plan_path = ROOT / 'config/workflow_plan.json'
        self.plan = json.loads(self.plan_path.read_text(encoding='utf-8')) if self.plan_path.exists() else default_plan()
        self.order = self.plan.get('tasks', default_plan()['tasks'])[:]
        self.selected = set(self.plan.get('selected', self.order))
        self.make_header()
        self.make_controls()
        self.make_reserved()
        self.make_body()
        self.load_context()
        self.render_queue()
        self.table.selection_set('navigation'); self.update_selection()
        self.restore_last_run()
        root.bind('<F8>', lambda _: self.stop())
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.maintenance_after = root.after(1000, self.maintenance_tick)
        root.bind('<Destroy>', self.destroyed, add='+')

    def destroyed(self, event):
        if event.widget is self.root:
            self.root.after_cancel(self.maintenance_after)

    def make_header(self):
        from workbench_theme import BrandBanner
        self.banner = BrandBanner(self.root, self.context, self.settings)
        self.banner.pack(fill='x', padx=20, pady=(12, 8))
        mode = ttk.Frame(self.root, padding=(28, 0, 28, 12)); mode.pack(fill='x')
        self.read_radio = ttk.Radiobutton(mode, text='只读检查', variable=self.execute, value=False, command=self.update_selection)
        self.read_radio.pack(side='left')
        self.execute_radio = ttk.Radiobutton(mode, text='实机执行', variable=self.execute, value=True, command=self.update_selection)
        self.execute_radio.pack(side='left', padx=(18, 0))
        ttk.Label(mode, text='网络等待', style='Muted.TLabel').pack(side='left', padx=(28, 8))
        self.network_combo = ttk.Combobox(mode, textvariable=self.network_wait, values=list(self.wait_choices), width=16, state='readonly')
        self.network_combo.pack(side='left')
        self.network_combo.bind('<<ComboboxSelected>>', self.save_network_wait)
        ttk.Label(mode, textvariable=self.mode_label, foreground=BLUE).pack(side='right')
        cache_bar = ttk.Frame(self.root, padding=(28, 0, 28, 8)); cache_bar.pack(fill='x')
        self.cleanup_toggle = ttk.Checkbutton(cache_bar, text='自动清理旧缓存', variable=self.auto_cleanup, command=self.save_cleanup_preference)
        self.cleanup_toggle.pack(side='left')
        self.cleanup_button = ttk.Button(cache_bar, text='清理旧缓存', command=self.clean_cache)
        self.cleanup_button.pack(side='left', padx=12)
        ttk.Label(cache_bar, text='保留今天和昨天；选用日期与未决记录始终保留', style='Muted.TLabel').pack(side='left')

    def make_reserved(self):
        reserved = ttk.Frame(self.root, padding=(24, 8, 24, 0)); reserved.pack(side='bottom', fill='x')
        ttk.Label(reserved, text='后续扩展', style='Muted.TLabel').pack(side='left', padx=(0, 18))
        for module in MODULES:
            if not module.worker:
                ttk.Button(reserved, text=module.name + ' · 预留', state='disabled').pack(side='left', padx=(0, 10))
        ttk.Label(reserved, text='当前不加入循环', style='Muted.TLabel').pack(side='right')

    def make_body(self):
        body = ttk.Frame(self.root, padding=(24, 0, 24, 0)); body.pack(fill='both', expand=True)
        body.columnconfigure(0, weight=6, uniform='workbench'); body.columnconfigure(1, weight=4, uniform='workbench'); body.rowconfigure(0, weight=1)
        left = ttk.Frame(body, style='Panel.TFrame', padding=14); left.grid(row=0, column=0, sticky='nsew', padx=(0, 14))
        bar = ttk.Frame(left, style='Panel.TFrame'); bar.pack(fill='x', pady=(0, 10))
        ttk.Label(bar, text='01  日常队列', style='Panel.TLabel', font=('Microsoft YaHei UI', 13, 'bold')).pack(side='left')
        ttk.Button(bar, text='上移', command=lambda: self.move(-1)).pack(side='right')
        ttk.Button(bar, text='下移', command=lambda: self.move(1)).pack(side='right', padx=6)
        frame = ttk.Frame(left, style='Panel.TFrame'); frame.pack(fill='both', expand=True)
        self.table = ttk.Treeview(frame, columns=('enabled', 'name', 'status', 'completion'), show='headings', selectmode='browse')
        for key, label, width in [('enabled', '加入', 50), ('name', '任务', 205), ('status', '本轮状态', 105), ('completion', '任务进度', 130)]:
            self.table.heading(key, text=label)
            self.table.column(key, width=width, minwidth=width if key == 'enabled' else 95, stretch=key != 'enabled')
        scroll = ttk.Scrollbar(frame, orient='vertical', command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set); scroll.pack(side='right', fill='y'); self.table.pack(fill='both', expand=True)
        self.table.tag_configure('reserved', foreground='#778292')
        self.table.tag_configure('verified', foreground=GREEN)
        self.table.tag_configure('needs_review', foreground=RED)
        self.table.tag_configure('blocked', foreground=AMBER)
        self.table.tag_configure('completed', foreground=GREEN)
        self.table.tag_configure('incomplete', foreground=AMBER)
        self.table.bind('<<TreeviewSelect>>', self.update_selection)
        self.table.bind('<ButtonRelease-1>', self.toggle_click)
        self.table.bind('<space>', lambda _: self.toggle_current())
        ttk.Label(left, textvariable=self.queue_summary, style='PanelMuted.TLabel').pack(anchor='w', pady=(10, 0))
        right = ttk.Frame(body); right.grid(row=0, column=1, sticky='nsew')
        details = ttk.Frame(right, style='Panel.TFrame', padding=18); details.pack(fill='both', expand=True)
        ttk.Label(details, textvariable=self.title, style='Panel.TLabel', font=('Microsoft YaHei UI', 14, 'bold')).pack(anchor='w')
        ttk.Label(details, textvariable=self.description, style='PanelMuted.TLabel', wraplength=430).pack(anchor='w', pady=(8, 12))
        steps_frame = ttk.Frame(details, style='Panel.TFrame'); steps_frame.pack(fill='both', expand=True)
        self.steps = tk.Text(steps_frame, width=1, height=5, wrap='word', bg=PANEL, fg=TEXT, relief='flat',
                             font=('Microsoft YaHei UI', 10), highlightthickness=0, spacing1=6, spacing3=6, takefocus=False)
        steps_scroll = ttk.Scrollbar(steps_frame, orient='vertical', command=self.steps.yview)
        self.steps.configure(yscrollcommand=steps_scroll.set)
        steps_scroll.pack(side='right', fill='y')
        self.steps.pack(side='left', fill='both', expand=True); self.steps.configure(state='disabled')
        ttk.Label(details, textvariable=self.completion, style='Panel.TLabel', wraplength=430).pack(fill='x', before=steps_frame, pady=(0, 10))
        log_frame = ttk.Frame(right, style='Panel.TFrame', padding=(15, 12)); log_frame.pack(side='bottom', fill='x', pady=(14, 0), before=details)
        ttk.Label(log_frame, text='03  最近判定', style='PanelMuted.TLabel').pack(anchor='w', pady=(0, 6))
        self.log = tk.Text(log_frame, width=1, height=4, wrap='word', bg=PANEL, fg=MUTED, relief='flat',
                           font=('Microsoft YaHei UI', 9), highlightthickness=0, state='disabled')
        self.log.pack(fill='both')

    def make_controls(self):
        footer = ttk.Frame(self.root, padding=(24, 16, 24, 18)); footer.pack(side='bottom', fill='x')
        self.progress = ttk.Progressbar(footer, maximum=100); self.progress.pack(fill='x', pady=(0, 10))
        ttk.Label(footer, textvariable=self.status, wraplength=1180).pack(anchor='w', pady=(0, 10))
        row = ttk.Frame(footer); row.pack(fill='x')
        self.queue_button = ttk.Button(row, text='识别勾选队列', style='Primary.TButton', command=self.start_queue)
        self.queue_button.pack(side='right')
        self.single_button = ttk.Button(row, text='只识别选中项', command=self.start_single); self.single_button.pack(side='right', padx=10)
        self.stop_button = ttk.Button(row, text='停止  F8', style='Stop.TButton', state='disabled', command=self.stop); self.stop_button.pack(side='left', padx=(0, 10))
        self.pause_button = ttk.Button(row, text='本段结束后暂停', state='disabled', command=self.pause); self.pause_button.pack(side='left')
        self.resume_button = ttk.Button(row, text='恢复本轮', state='disabled', command=self.resume); self.resume_button.pack(side='left', padx=10)
        self.continue_button = ttk.Button(row, text='继续未运行项', state='disabled', command=self.continue_remaining)
        self.continue_button.pack(side='left', padx=4)
        self.process_button = ttk.Button(row, text='过程窗口', command=self.open_process_window)
        self.process_button.pack(side='left')

    def load_context(self):
        self.date_ready = False
        try:
            previous = getattr(self, 'daily', {}).get('cache_date')
            if self.process is None and self.settings_window is None:
                change = synchronize_date(ROOT)
                if change['changed']:
                    self.task_reports.clear(); self.row_status.clear()
                    for key in self.table.get_children():
                        self.table.set(key, 'status', '待运行'); self.table.item(key, tags=())
                    self.status.set('已按北京时间08:00切换游戏日，历史缓存保留，今日完成状态重新核对。')
            self.date_ready = require_current_cache(ROOT) == game_day().isoformat()
            self.daily = load_local_config(ROOT / 'config/daily_tasks.json')
            self.event = load_local_config(ROOT / 'config/event_and_rewards.json')
            day = self.daily['cache_date']
            if previous and previous != day:
                self.task_reports.clear()
            count = len(list((ROOT / 'runtime').glob(day + '_*.json')))
            self.context.set('游戏日  ' + game_day().isoformat() + '（北京时间08:00刷新） / 数据缓存 ' + day + f' · {count}项')
        except Exception as error:
            self.daily, self.event = {}, {}
            self.context.set('游戏日 ' + game_day().isoformat() + ' / 换日暂缓，请核对旧回执')
            self.status.set(str(error))
        self.refresh_task_progress()

    def refresh_task_progress(self):
        for module in MODULES:
            if not module.worker:
                continue
            progress = self.progress_reader.assess(module.id, self.task_reports.get(module.id))
            previous = self.task_progress.get(module.id)
            self.task_progress[module.id] = progress
            if previous != progress and module.id in self.task_reports:
                self.record_event(LogEvent(module.name, 'warning' if progress['state'] == 'unknown' else 'info', progress_text(progress)))
            if self.table.exists(module.id):
                self.table.set(module.id, 'completion', progress['label'])
        selection = self.table.selection()
        if selection and selection[0] in self.task_progress:
            self.completion.set(progress_text(self.task_progress[selection[0]]))

    def capture_task_report(self, task_id, output, failed=False):
        if task_id not in BY_ID:
            return
        report = None
        for name in ('run.json', 'inspection.json'):
            try:
                report = json.loads((Path(output) / name).read_text(encoding='utf-8'))
                if not isinstance(report, dict):
                    report = None
                break
            except (OSError, ValueError):
                pass
        if failed:
            report = dict(report or {}, error=(report or {}).get('error') or '本次执行未通过核对')
        if report is not None:
            try:
                receipt = json.loads((Path(output) / 'worker_result.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                receipt = {}
            report.setdefault('cache_date', receipt.get('cache_date') or getattr(self, 'running_cache_date', None))
            self.task_reports[task_id] = report

    def save_cleanup_preference(self):
        try:
            set_automatic_cleanup(ROOT, self.auto_cleanup.get())
            self.cleanup_day = None
            self.status.set('自动清理已' + ('开启' if self.auto_cleanup.get() else '关闭') + '；保留今天、昨天、选用日期及未决记录。')
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            self.auto_cleanup.set(automatic_cleanup_enabled(ROOT))
            self.status.set('缓存清理设置未保存：' + str(error))

    def clean_cache(self, automatic=False):
        if self.process or (self.settings_window is not None and self.settings_window.winfo_exists()):
            if not automatic:
                self.status.set('运行或设置中暂缓清理，结束后再检查。')
            return False
        result = clear_old_cache(ROOT)
        text = cleanup_summary(result)
        if not automatic or result['deleted'] or result['errors']:
            self.status.set(text); self.append_log(text)
            self.record_event(LogEvent('缓存维护', 'warning' if result['errors'] else 'info', text))
        self.load_context()
        return not result['busy'] and not result['errors']

    def maintenance_tick(self):
        self.load_context()
        if self.auto_cleanup.get() and self.date_ready and self.cleanup_day != game_day():
            if self.clean_cache(automatic=True):
                self.cleanup_day = game_day()
        self.maintenance_after = self.root.after(60000, self.maintenance_tick)

    def render_queue(self):
        current = self.table.selection()
        self.table.delete(*self.table.get_children())
        order = [key for key in self.order if key in BY_ID and BY_ID[key].worker]
        self.queue_summary.set(f'已选 {len(self.selected)} / {len(order)} 项 · 点击加入列或按空格勾选')
        for index, key in enumerate(order):
            module = BY_ID[key]
            status = self.row_status.get(key, '待运行')
            self.table.insert('', 'end', iid=key, values=('—' if not module.worker else '☑' if key in self.selected else '☐', f'{index + 1:02d}  {module.name}', status, self.task_progress.get(key, {}).get('label', '待核对')),
                              tags=('reserved',) if not module.worker else ())
        if current and self.table.exists(current[0]):
            self.table.selection_set(current[0]); self.table.see(current[0])

    def update_selection(self, event=None):
        selection = self.table.selection()
        module = BY_ID.get(selection[0]) if selection else None
        running = self.process is not None
        if module:
            self.title.set('02  ' + module.name)
            self.description.set(module.description)
            if module.id in self.task_progress:
                self.completion.set(progress_text(self.task_progress[module.id]))
            self.steps.configure(state='normal'); self.steps.delete('1.0', 'end')
            self.steps.insert('end', '\n'.join(f'{i + 1:02d}   {step}' for i, step in enumerate(module.steps)) if module.worker else module.reserved_reason)
            self.steps.configure(state='disabled')
            if not module.worker:
                self.scope.set('停用入口 · 按后续样本扩展')
            elif module.id == 'navigation':
                self.scope.set('只读：仅识别当前画面，不会关闭弹窗。\n实机：返回主页后顺查并领取每日／每周可领奖励；不启动其他日常段。')
            elif module.id == 'guild':
                enabled = self.event.get('guild', {}).get('enabled', False)
                self.scope.set(('配置已启用' if enabled else '配置未启用 · 日常设置 → 公会签到') + '\n加入队列与启用分别设置；本游戏日只进入一次，邮箱手动。')
            elif module.id == 'mirror':
                cfg = self.daily.get('mirror', {})
                self.scope.set(f"本周期最多 {cfg.get('max_battles', '—')} 场 · 总额度 {cfg.get('resource_budget', '—')}\n保持当前阵容；实测自动战斗入口，赛季末跳过并返回主页。")
            elif module.id == 'event':
                cfg = self.event.get('event', {})
                method = '快速战斗' if cfg.get('battle_method') == 'quick' else '连续战斗'
                mode = '挑战' if cfg.get('mode') == 'challenge' else '普通'
                self.scope.set(f"配置 {'启用' if cfg.get('enabled') else '停用'} · {mode} / {method} · {cfg.get('runs', 0)} 次\n普通与挑战共享AP总额度 {cfg.get('ap_budget', 0)}；日常设置 → 活动战斗。")
            elif module.id == 'hunting':
                from hunting_resources import resource_text
                self.scope.set('自动读取每日任务进度；固定1次只用免费体力。\n米饭：'+resource_text(ROOT,'rice')+'\n火把：'+resource_text(ROOT,'torch'))
            elif module.id == 'task_rewards':
                self.scope.set('每次检查每日 + 每周\n只领取已完成项目和当前已达标区间奖励。')
            elif module.id == 'equipment':
                self.scope.set('按游戏日核对精炼、分解与初始化；已完成不重做。\n核对UR20分候选、R单选、未锁未穿戴及恰好1件；未读清即停步。')
            elif module.id == 'rewards':
                self.scope.set('本期两本通行证 · 任务后复查等级\n两本任务与等级奖励分别复查；领取后核对回执。')
            else:
                self.scope.set('读取日期与本地缓存 · 保持当前阵容\n未知画面或未决操作停步核对。')
            self.steps.configure(state='normal')
            self.steps.insert('end', '\n\n资源与执行条件\n' + self.scope.get())
            self.steps.configure(state='disabled')
        self.mode_label.set('实机操作 · 按配置和账本逐段核对' if self.execute.get() else '只读模式 · 不发送游戏输入')
        self.queue_button.configure(text='连续执行勾选队列' if self.execute.get() else '只读检查勾选队列', state='disabled' if running else 'normal')
        self.single_button.configure(text='执行选中单段' if self.execute.get() else '只识别选中项', state='normal' if module and module.worker and not running else 'disabled')
        self.read_radio.configure(state='disabled' if running else 'normal'); self.execute_radio.configure(state='disabled' if running else 'normal')
        self.network_combo.configure(state='disabled' if running else 'readonly')
        self.cleanup_button.configure(state='disabled' if running else 'normal')
        self.cleanup_toggle.configure(state='disabled' if running else 'normal')

    def save_network_wait(self, event=None):
        if self.process:
            return
        path = ROOT / 'config/runtime_preferences.json'
        try:
            values = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            values['network_wait_seconds'] = self.wait_choices[self.network_wait.get()]
            from workflow_runner import save
            save(path, values)
            self.status.set(f"每次页面响应最多等待 {values['network_wait_seconds']} 秒；超时保留未决记录，不自动重发。")
        except (OSError, ValueError, KeyError) as error:
            self.status.set('网络等待设置未保存：' + str(error))

    def save_plan(self):
        self.plan = {'schema_version': 1, 'tasks': self.order, 'selected': [key for key in self.order if key in self.selected]}
        self.plan_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.plan_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.plan, ensure_ascii=False, indent=2), encoding='utf-8'); temporary.replace(self.plan_path)

    def toggle_click(self, event):
        if self.table.identify_column(event.x) == '#1':
            self.toggle_current()

    def toggle_current(self):
        if self.process or not self.table.selection():
            return
        key = self.table.selection()[0]
        if not BY_ID[key].worker:
            return
        self.selected.remove(key) if key in self.selected else self.selected.add(key)
        self.save_plan(); self.render_queue()

    def move(self, delta):
        if self.process or not self.table.selection():
            return
        key = self.table.selection()[0]
        if key not in self.order:
            return
        old = self.order.index(key); new = max(0, min(len(self.order) - 1, old + delta))
        self.order[old], self.order[new] = self.order[new], self.order[old]
        self.save_plan(); self.render_queue()

    def settings(self):
        if self.process:
            self.status.set('运行中配置已锁定；本段结束后暂停再核对。'); return
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.deiconify(); self.settings_window.lift(); self.settings_window.focus_force()
            return
        self.load_context()
        from daily_settings import SettingsWindow
        window = self.settings_window = tk.Toplevel(self.root)
        self.settings_editor = SettingsWindow(window, ROOT / 'config/daily_tasks.json')
        def destroyed(event):
            if event.widget is window:
                self.settings_window = self.settings_editor = None
                if self.banner.winfo_exists():
                    self.load_context()
        window.bind('<Destroy>', destroyed,add='+')

    def open_process_window(self):
        if self.process_view is not None and self.process_view.window.winfo_exists():
            self.process_view.context.set(self.log_context)
            self.process_view.show()
            return
        self.process_view = ProcessWindow(self.root, self.events, self.open_logs, self.log_context)

    def record_event(self, event):
        self.events.append(event)
        if self.process_view is not None and self.process_view.window.winfo_exists():
            self.process_view.append(event)

    def collect_receipts(self, final=False):
        if self.output is None:
            return
        if self.receipt_log is None or self.receipt_log.output != self.output:
            self.receipt_log = ReceiptLog(self.output, self.running_task_id)
        for event in self.receipt_log.collect(final=final):
            self.record_event(event)

    def start_queue(self):
        plan = {'schema_version': 1, 'tasks': self.order, 'selected': list(self.selected)}
        try:
            validate_plan(plan)
        except ValueError as error:
            self.status.set(str(error)); return
        self.launch('workflow', plan=plan)

    def start_single(self):
        if self.table.selection():
            self.launch(self.table.selection()[0])

    def launch(self, task_id, plan=None, resume=False):
        if self.process:
            return
        if task_id != 'workflow' and not BY_ID[task_id].worker:
            return
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.status.set('请先保存或关闭日常设置，再启动任务。'); return
        self.load_context()
        # Execute workers first return home, then enforce the current cache date.
        if self.execute.get() and task_id == 'guild':
            from event_reward_rules import guild_disabled_reason
            from module_catalog import validate_plan
            tasks = validate_plan(plan or self.plan) if task_id == 'workflow' else [task_id]
            reason = guild_disabled_reason(self.event, tasks, True)
            if reason:
                self.status.set(reason)
                self.settings()
                self.settings_editor.notebook.select(self.settings_editor.guild_tab)
                return
        if not resume:
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            self.output = ROOT / 'evidence/workbench_runs' / f'{stamp}_{task_id}'
            self.output.mkdir(parents=True, exist_ok=False)
        self.running_task_id = task_id
        self.running_cache_date = self.daily.get('cache_date')
        if task_id != 'workflow':
            self.task_reports.pop(task_id, None)
        self.stop_file, self.pause_file = self.output / 'stop.requested', self.output / 'pause.requested'
        if resume:
            self.stop_file.unlink(missing_ok=True); self.pause_file.unlink(missing_ok=True)
        args = ['--worker', task_id, '--output', str(self.output)]
        if self.execute.get():
            args.append('--execute')
        if plan:
            manifest = self.output / 'plan.json'; manifest.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
            args.extend(['--manifest', str(manifest)])
        if task_id == 'workflow':
            from workflow_runner import save
            save(ROOT / 'runtime/workbench_last_run.json', {'output': str(self.output)})
        if resume:
            args.append('--resume')
        base = [sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, '-X', 'utf8', str(ROOT / 'tools/assistant_app.py')]
        env = dict(os.environ, BD2_STOP_FILE=str(self.stop_file), BD2_PAUSE_FILE=str(self.pause_file), PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        self.record_event(LogEvent('作战台', 'info', ('恢复本轮' if resume else '开始本轮') + (' · 实机执行' if self.execute.get() else ' · 只读检查') + ' · ' + self.output.name))
        self.log_context = '实机执行' if self.execute.get() else '只读检查 · 不发送游戏输入'
        self.open_process_window()
        self.root.update_idletasks()
        with (self.output / 'process.log').open('ab') as log:
            self.process = subprocess.Popen(base + args, cwd=str(ROOT), env=env, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
        self.status.set('正在逐段核对队列…' if task_id == 'workflow' else '正在运行：' + BY_ID[task_id].name)
        self.stop_button.configure(state='normal'); self.pause_button.configure(state='normal' if task_id == 'workflow' else 'disabled')
        self.resume_button.configure(state='disabled'); self.continue_button.configure(state='disabled')
        self.update_selection(); self.root.after(200, self.poll)

    def append_log(self, text):
        if text == self.last_log:
            return
        self.last_log = text
        self.log.configure(state='normal'); self.log.insert('end', datetime.now().strftime('%H:%M:%S') + '  ' + text + '\n')
        self.log.see('end'); self.log.configure(state='disabled')
        # Recent summary remains compact; detailed receipts go to the viewer.
        if int(self.log.index('end-1c').split('.')[0]) > 100:
            self.log.configure(state='normal'); self.log.delete('1.0', '50.0'); self.log.configure(state='disabled')

    def poll(self):
        if self.process is None:
            return
        self.collect_receipts()
        path = self.output / 'workflow.json'
        snapshot = None
        if path.exists():
            try:
                snapshot = json.loads(path.read_text(encoding='utf-8'))
                labels = {'queued': '待运行', 'running': '正在核对', 'verified': '已核实', 'inspected': '已识别', 'needs_review': '待核对','skipped':'已跳过', 'blocked':'受阻，未执行'}
                for item in snapshot['tasks']:
                    if self.table.exists(item['id']):
                        if item.get('output'):
                            self.capture_task_report(item['id'], item['output'], item['status'] in ('needs_review', 'blocked'))
                        values = list(self.table.item(item['id'], 'values')); values[2] = labels.get(item['status'], item['status'])
                        self.table.item(item['id'], values=values, tags=(item['status'],))
                        self.row_status[item['id']] = values[2]
                        if item['status'] == 'running':
                            self.table.selection_set(item['id']); self.table.see(item['id']); self.update_selection()
                            self.status.set('正在执行：' + BY_ID[item['id']].name)
                self.progress['value'] = 100 * snapshot.get('cursor', 0) / max(1, len(snapshot['tasks']))
                if snapshot.get('message'):
                    self.status.set(snapshot['message'])
                self.append_log(str(snapshot.get('reason') or self.status.get()))
                self.refresh_task_progress()
            except (OSError, ValueError, KeyError):
                pass
        code = self.process.poll()
        if code is None:
            try:
                current = next((item for item in snapshot['tasks'] if item['status'] == 'running'), None) if snapshot else None
                report_path = Path(current['output']) / 'run.json' if current else self.output / 'run.json'
                waiting = json.loads(report_path.read_text(encoding='utf-8')).get('waiting')
                if waiting:
                    message = waiting.get('message', str(waiting)) if isinstance(waiting, dict) else str(waiting)
                    self.status.set(message); self.append_log(message)
            except (OSError, ValueError, KeyError):
                pass
            self.root.after(250, self.poll); return
        self.process = None
        self.log_context = '本轮已结束 · 可回看过程'
        if self.process_view is not None and self.process_view.window.winfo_exists():
            self.process_view.context.set(self.log_context)
        self.collect_receipts(final=True)
        self.record_event(LogEvent('作战台', 'error' if code else 'info', f'执行器已退出，退出码 {code}；完成情况以回执判断。'))
        from task_feedback import worker_feedback
        try:
            self.status.set(worker_feedback(self.output, self.running_task_id))
        except (OSError, ValueError, KeyError) as error:
            self.status.set('执行回执无法读取，请打开运行记录核对：' + str(error))
        if snapshot is None and self.running_task_id != 'workflow':
            from workflow_runner import evaluate_result
            try:
                report = json.loads((self.output / 'run.json').read_text(encoding='utf-8'))
                state = evaluate_result(report, code, self.execute.get())
                label = {'verified': '已核实', 'inspected': '已识别', 'needs_review': '待核对','skipped':'已跳过'}[state]
                values = list(self.table.item(self.running_task_id, 'values')); values[2] = label
                self.table.item(self.running_task_id, values=values, tags=(state,))
                self.row_status[self.running_task_id] = label
                self.progress['value'] = 100 if state in ('verified', 'inspected', 'skipped') else 0
            except (OSError, ValueError, KeyError):
                pass
            self.capture_task_report(self.running_task_id, self.output, bool(code))
        self.refresh_task_progress()
        if self.running_task_id in self.task_progress:
            verdict = progress_text(self.task_progress[self.running_task_id])
            self.status.set(self.status.get() + ' ' + verdict)
            self.record_event(LogEvent(BY_ID[self.running_task_id].name, 'info', verdict))
        if snapshot:
            state = snapshot['status']
            if state == 'paused':
                self.status.set('已在模块边界暂停，可恢复本轮。')
            elif state == 'inspected':
                self.status.set('勾选队列只读检查结束，没有点击游戏。')
            elif state == 'complete':
                skipped = [BY_ID[item['id']].name for item in snapshot['tasks'] if item['status'] == 'skipped']
                self.status.set('本轮队列已结束，已返回主菜单。' + ('赛季末跳过：' + '、'.join(skipped) + '。' if skipped else '各段结果已核实。'))
            elif state == 'complete_with_errors':
                summary = snapshot['summary']
                self.status.set(f"执行结束，有错误：成功 {summary['success']}，失败 {summary['failed']}，受阻 {summary['blocked']}，跳过 {summary['skipped']}。详情见执行过程。")
            elif state == 'stopped':
                self.status.set(snapshot.get('reason') or '已停止，未继续后续项目。')
            elif state == 'needs_review' and snapshot.get('reason'):
                self.status.set(snapshot['reason'])
            self.resume_button.configure(state='normal' if state in ('paused', 'stopped', 'needs_review') and not snapshot.get('traversed') else 'disabled')
            self.continue_button.configure(state='normal' if state in ('paused', 'stopped', 'needs_review') and any(item['status'] == 'queued' for item in snapshot['tasks']) else 'disabled')
        self.append_log(self.status.get()); self.stop_button.configure(state='disabled'); self.pause_button.configure(state='disabled')
        missing_report = not any((self.output / name).is_file() for name in ('run.json', 'inspection.json'))
        level = 'error' if code or missing_report else 'warning' if snapshot and snapshot.get('status') in ('needs_review', 'stopped') else 'info'
        self.record_event(LogEvent('作战台', level, self.status.get()))
        self.update_selection()
        if self.closing:
            self.root.destroy()

    def pause(self):
        if self.process and self.running_task_id == 'workflow':
            self.pause_file.write_text('pause at module boundary\n', encoding='utf-8')
            self.status.set('已请求暂停，当前段结束后停止交接下一段。')
            self.record_event(LogEvent('作战台', 'wait', self.status.get()))

    def continue_remaining(self):
        if self.process or not self.output:
            return
        from workflow_runner import continuation_plan
        try:
            plan = continuation_plan(self.output / 'workflow.json')
            if plan['continuation']['execute'] != self.execute.get():
                raise RuntimeError('继续未运行项需选择原执行模式。')
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            self.status.set(str(error)); return
        self.launch('workflow', plan=plan)

    def resume(self):
        if not self.process and self.output and (self.output / 'workflow.json').exists():
            previous = json.loads((self.output / 'workflow.json').read_text(encoding='utf-8'))
            if previous['identity'].get('cache_date') != game_day().isoformat():
                self.status.set('旧游戏日队列已保留，不能恢复到今天；请重新启动本轮检查。'); return
            if previous['execute'] != self.execute.get():
                self.status.set('恢复本轮需选择原来的' + ('实机执行' if previous['execute'] else '只读检查') + '模式。')
                return
            from workflow_runner import config_identity
            identity, hashes = config_identity(ROOT)
            if previous['identity'] != identity or previous['config_hashes'] != hashes:
                self.status.set('配置已改变；原队列保持。请点“继续未运行项”建立新队列，原失败项不会重试。'); return
            self.launch('workflow', resume=True)

    def restore_last_run(self):
        path = ROOT / 'runtime/workbench_last_run.json'
        if not path.is_file():
            return
        try:
            output = Path(json.loads(path.read_text(encoding='utf-8'))['output']).resolve(strict=True)
            output.relative_to((ROOT / 'evidence/workbench_runs').resolve())
            state = json.loads((output / 'workflow.json').read_text(encoding='utf-8'))
            if state['identity'].get('cache_date') != game_day().isoformat():
                return
            if state['status'] in ('paused', 'stopped', 'needs_review'):
                self.output = output
                self.resume_button.configure(state='normal')
                self.continue_button.configure(state='normal' if any(item['status'] == 'queued' for item in state['tasks']) else 'disabled')
                self.status.set('发现中断队列：' + str(state.get('reason') or '在模块边界暂停') + '。配置改变时可点“继续未运行项”，原失败项不重试。')
        except (OSError, ValueError, KeyError):
            self.status.set('上次队列记录无法恢复，请在运行记录中核对。')

    def stop(self):
        if self.process and self.stop_file:
            self.stop_file.write_text('user requested stop\n', encoding='utf-8')
            self.status.set('停止请求已发送，等待当前模块保存回执。')
            self.record_event(LogEvent('作战台', 'wait', self.status.get()))

    def close(self):
        if self.process:
            self.closing = True; self.stop(); return
        self.root.destroy()

    def open_logs(self):
        target = self.output or ROOT / 'evidence'
        target.mkdir(parents=True, exist_ok=True); os.startfile(str(target))
