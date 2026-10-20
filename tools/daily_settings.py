"""Small editable settings window; saving never launches a game action."""
import copy
import ctypes
import hashlib
import json
import os
import uuid
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from daily_tasks import load_config, validate_config, hunt_free_only, HUNT_DIFFICULTIES, HUNT_TARGETS, STONE_STAGES, hunt_target_key
from m00_recognize import ROOT

try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except (AttributeError, OSError):
    pass


class SettingsWindow:
    def __init__(self, root, path):
        self.root, self.path = root, Path(path).resolve()
        self.original = self.path.read_bytes()
        self.config = load_config(self.path)
        self.event_path = self.path.with_name('event_and_rewards.json')
        self.event_original = self.event_path.read_bytes() if self.event_path.exists() else None
        self.event_config = json.loads(self.event_original) if self.event_original else None
        self.fields = {}
        root.title('棕色尘埃2 · 日常设置')
        root.geometry('820x780')
        root.minsize(760, 740)
        from settings_theme import apply_settings_theme
        apply_settings_theme(root)
        frame = ttk.Frame(root, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='日常设置', font=('Microsoft YaHei UI', 18, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='保存后用于下一次运行；加入队列与启用模块分别设置。').pack(anchor='w', pady=(4, 10))
        identity = ttk.Frame(frame)
        identity.pack(fill='x')
        self.entry(identity, 'cache_date', '数据缓存日期', self.config['cache_date'], 0)
        ttk.Label(frame, text='按日期读取本地进度，不读取账号。北京时间每天08:00自动换日；历史缓存保留，新一天重新核对完成状态。').pack(anchor='w', pady=(5, 10))
        notebook = ttk.Notebook(frame)
        self.notebook = notebook
        notebook.pack(fill='both', expand=True)
        hunt, goddess, mirror = [ttk.Frame(notebook, padding=15) for _ in range(3)]
        notebook.add(hunt, text='快速狩猎')
        notebook.add(goddess, text='女神像')
        notebook.add(mirror, text='镜中之战')
        self.build_hunt(hunt)
        self.build_goddess(goddess)
        self.build_mirror(mirror)
        self.guild_tab = ttk.Frame(notebook, padding=15)
        notebook.add(self.guild_tab, text='公会签到')
        self.check(self.guild_tab, 'guild_enabled', '启用公会签到',
                   bool(self.event_config and self.event_config['guild']['enabled']), 0)
        if self.event_config is None:
            self.fields['guild_enabled'].set(False)
        ttk.Label(self.guild_tab, text='启用后，还需在主界面勾选加入队列。每个游戏日只进入一次，核对签到回执后返回主页。\n保存不会启动游戏操作；公会奖励邮箱仍由你手动领取。',
                  wraplength=650, justify='left').grid(row=1, column=0, columnspan=2, sticky='w', pady=16)
        self.activity_tab = ttk.Frame(notebook, padding=15)
        notebook.add(self.activity_tab, text='活动战斗')
        self.build_activity(self.activity_tab)
        bottom = ttk.Frame(frame)
        bottom.pack(side='bottom', fill='x', before=notebook, pady=(14, 0))
        self.saved = tk.StringVar(value='未启动游戏操作')
        ttk.Label(bottom, textvariable=self.saved).pack(side='left')
        ttk.Button(bottom, text='保存设置', command=self.save).pack(side='right')

    def entry(self, parent, key, label, value, row, choices=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', padx=(0, 14), pady=6)
        var = tk.StringVar(value='' if value is None else str(value))
        self.fields[key] = var
        widget = ttk.Combobox(parent, textvariable=var, values=choices) if choices else ttk.Entry(parent, textvariable=var)
        if key in ('hunt_difficulty','hunt_category','daily_status','cache_date','event_mode','event_method'):
            widget.configure(state='readonly')
        widget.grid(row=row, column=1, sticky='ew', pady=6)
        parent.columnconfigure(1, weight=1)
        return var

    def check(self, parent, key, label, value, row):
        var = tk.BooleanVar(value=value)
        self.fields[key] = var
        ttk.Checkbutton(parent, text=label, variable=var).grid(row=row, column=0, columnspan=2, sticky='w', pady=6)

    def build_hunt(self, parent):
        c = self.config['hunting']
        self.check(parent, 'hunt_enabled', '启用快速狩猎', c['enabled'], 0)
        ttk.Label(parent, text='固定日常：1 次。额外狩猎另设，不会改动这一次。').grid(row=1, column=0, columnspan=2, sticky='w')
        self.fields['daily_status'] = tk.StringVar(value='运行时自动检查每日任务页')
        ttk.Label(parent, text='每日狩猎进度').grid(row=2,column=0,sticky='w',pady=6)
        ttk.Label(parent,textvariable=self.fields['daily_status']).grid(row=2,column=1,sticky='w',pady=6)
        self.entry(parent, 'hunt_category', '固定一次的类别', c['daily_target']['category'], 3, ['狩猎场'])
        ttk.Label(parent,text='固定关卡：1 · 野猪洞穴（1次）').grid(row=4,column=0,columnspan=2,sticky='w',pady=6)
        self.entry(parent, 'hunt_difficulty', '固定一次的难度', c['daily_target']['difficulty'], 5, HUNT_DIFFICULTIES)
        self.resource_labels = {}
        for row, resource, name in ((6,'rice','米饭（自动读取）'),(7,'torch','火把（自动读取）')):
            value = self.resource_labels[resource] = tk.StringVar()
            ttk.Label(parent,text=name).grid(row=row,column=0,sticky='w',pady=6)
            ttk.Label(parent,textvariable=value,wraplength=430).grid(row=row,column=1,sticky='w',pady=6)
        ttk.Label(parent, text='固定日常：只使用免费体力；次数固定1次。').grid(row=8,column=0,columnspan=2,sticky='w',pady=6)
        ttk.Label(parent, text='资源从游戏画面自动读取，提交前重读，结算后刷新；额外狩猎单独设置。',wraplength=650).grid(row=9,column=0,columnspan=2,sticky='w')
        self.refresh_hunting_display()
        def cancel_refresh(event):
            if event.widget is self.root:
                try:self.root.after_cancel(self.hunting_refresh_job)
                except (tk.TclError,AttributeError):pass
        self.root.bind('<Destroy>',cancel_refresh,add='+')
        ttk.Button(parent, text='逐项设置额外狩猎（难度／次数／免费体力）', command=self.extra_plans).grid(row=10, column=0, columnspan=2, sticky='w', pady=10)

    def refresh_hunting_display(self):
        from hunting_resources import resource_text
        from local_cache import cache_path
        root = self.path.parent.parent
        for resource,var in self.resource_labels.items():var.set(resource_text(root,resource))
        try:
            data=json.loads(cache_path(root,self.config).read_text(encoding='utf-8'))
            evidence=data['hunting']['daily_check']
            value={'done':'已完成（自动识别1/1）','not_done':'未完成（自动识别0/1）'}.get(evidence.get('status'),'待自动核对')
            if data['hunting'].get('daily_done') and data['hunting'].get('runs',{}).get('daily',0)>=1:value='已完成（狩猎回执已核对）'
            self.fields['daily_status'].set(value if evidence.get('cache_date')==self.config['cache_date'] else '待自动核对')
        except (OSError,ValueError,KeyError):pass
        self.hunting_refresh_job=self.root.after(1000,self.refresh_hunting_display)

    def build_goddess(self, parent):
        c = self.config['goddess']
        self.check(parent, 'goddess_enabled', '启用女神像互动', c['enabled'], 0)
        self.check(parent, 'positioned', '运行前，我会让角色站在女神像旁', c['user_positioned_near_statue'], 1)
        self.check(parent, 'check_in', '签到：识别拍手图标后互动一次', 'check_in' in c['actions'], 2)
        self.check(parent, 'daily_reward', '日常奖励：其他挑战完成、可互动时再领取', 'daily_reward' in c['actions'], 3)
        text = ('每次互动后重新检查图标，每天两类各一次。\n\n'
                '录像中，签到后拍手变成感叹号，领奖后入口消失。\n'
                '剩余奖励尚未开放时保留待领状态，可在挑战后再检查。\n\n'
                '工具不会移动角色寻找女神像。')
        ttk.Label(parent, text=text, wraplength=650, justify='left').grid(row=4, column=0, columnspan=2, sticky='nw', pady=20)

    def build_mirror(self, parent):
        c = self.config['mirror']
        self.check(parent, 'mirror_enabled', '启用镜中之战', c['enabled'], 0)
        self.entry(parent, 'mirror_battles', '最多战斗次数', c['max_battles'], 1)
        self.entry(parent, 'mirror_budget', '鲜血鸡尾酒总额度', c['resource_budget'], 2)
        self.entry(parent, 'mirror_multiplier', '游戏内消耗倍率', c['resource_multiplier'], 3)
        self.check(parent, 'mirror_free', '只使用免费鲜血鸡尾酒', c['free_only'], 4)
        self.check(parent, 'mirror_skip', '自动跳过战斗', c['auto_skip'], 5)
        ttk.Label(parent, text=('沿用你选好的阵容；次数或额度先到上限就停止。\n\n'
                               '可从主菜单、镜中大厅或准备页开始，自动调整倍率（1～40）。\n'
                               '每次开1场，结算后累计次数和额度；完成后返回主菜单。\n'
                               '录像的 40 倍不作为默认值；首次预填 1 场、1 额度、1 倍。'),
                  wraplength=650, justify='left').grid(row=6, column=0, columnspan=2, sticky='nw', pady=20)

    def build_activity(self, parent):
        c = (self.event_config or {}).get('event', {})
        self.check(parent, 'event_enabled', '启用活动战斗', c.get('enabled', False), 0)
        self.entry(parent, 'event_mode', '难度', '普通' if c.get('mode', 'normal') == 'normal' else '挑战', 1, ['普通', '挑战'])
        method = c.get('battle_method', 'quick' if c.get('quick_battle', {}).get('enabled') else 'continuous')
        self.entry(parent, 'event_method', '战斗方式', '快速战斗' if method == 'quick' else '连续战斗', 2, ['连续战斗', '快速战斗'])
        self.entry(parent, 'event_runs', '本日该难度目标总次数', c.get('runs', 0), 3)
        self.entry(parent, 'event_budget', '普通／挑战共享AP总额度', c.get('ap_budget', 0), 4)
        self.check(parent, 'event_free', '仅使用免费活动AP（关闭可使用已有额外体力）', not c.get('use_owned_extra_ap', False), 5)
        ttk.Label(parent, text='实际AP余额由画面自动读取，不手动填写；总额度是你允许本日活动消耗的上限。\n普通与挑战共用AP和累计额度；更换方式不会重复已核实的战斗次数。\n快速战斗需要游戏已解锁；未找到对应按钮时会停步提示。\n保持原阵容，不购买体力；保存设置不会启动战斗。', wraplength=650, justify='left').grid(row=6, column=0, columnspan=2, sticky='w', pady=16)
        self.activity_balance = tk.StringVar()
        ttk.Label(parent, textvariable=self.activity_balance, wraplength=650).grid(row=7, column=0, columnspan=2, sticky='w', pady=8)
        def refresh():
            from event_resources import balance_text
            self.activity_balance.set(balance_text(self.path.parent.parent))
            self.activity_refresh_job = self.root.after(1000, refresh)
        refresh()
        def cancel(event):
            if event.widget is self.root:
                try: self.root.after_cancel(self.activity_refresh_job)
                except (tk.TclError, AttributeError): pass
        self.root.bind('<Destroy>', cancel, add='+')

    def collect_event(self, config):
        from event_reward_rules import validate_config
        c = copy.deepcopy(config)
        c['guild']['enabled'] = bool(self.fields['guild_enabled'].get())
        e = c['event']
        values = dict(enabled=bool(self.fields['event_enabled'].get()),
                 mode={'普通':'normal', '挑战':'challenge'}[self.fields['event_mode'].get()],
                 battle_method={'连续战斗':'continuous', '快速战斗':'quick'}[self.fields['event_method'].get()],
                 runs=int(self.fields['event_runs'].get()), ap_budget=int(self.fields['event_budget'].get()),
                 use_owned_extra_ap=not bool(self.fields['event_free'].get()))
        baseline = {key: e.get(key, 'quick' if e.get('quick_battle', {}).get('enabled') else 'continuous')
                    if key == 'battle_method' else e.get(key) for key in values}
        if values != baseline:
            e.update(values)
        if e['enabled'] and e.get('event_id') == 'configure_current_event':
            e['event_id'] = 'auto_current'
        return validate_config(c)

    def extra_plans(self):
        existing = getattr(self, 'plan_editor', {}).get('window')
        if existing is not None and existing.winfo_exists():
            existing.deiconify(); existing.lift(); existing.focus_set()
            return
        window = tk.Toplevel(self.root)
        from settings_theme import BG
        window.configure(bg=BG)
        window.title('额外狩猎计划')
        window.geometry('960x660')
        window.minsize(920,640)
        frame = ttk.Frame(window, padding=15)
        frame.pack(fill='both', expand=True)
        tree = ttk.Treeview(frame, style='Settings.Treeview', columns=('category','stage','difficulty','runs','free'), show='headings', height=7)
        for key, label in [('category','类别'), ('stage','目标／圣石选择'), ('difficulty','难度'), ('runs','次数'), ('free','只用免费体力')]:
            tree.heading(key, text=label)
            tree.column(key, width=130)
        tree.pack(fill='both', expand=True)
        ttk.Label(frame,text='列表为空时不执行额外狩猎；每日固定1次会独立自动检查。').pack(anchor='w',pady=4)
        plans = {plan['id']:copy.deepcopy(plan) for plan in self.config['hunting']['extra_plans']}
        def display(plan):
            return (plan['category'], '库存最少' if plan.get('target_mode')=='lowest_stock' else plan['stage'],
                    plan['difficulty'], '用完免费' if plan.get('runs_mode')=='all_free' else plan['runs'], '是' if hunt_free_only(self.config['hunting'],plan) else '否')
        for plan in plans.values():
            tree.insert('', 'end', iid=plan['id'], values=display(plan))
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=12)
        variables, widgets = [], []
        for i, (label, value, choices) in enumerate([
            ('类别','狩猎场',('狩猎场','金币','史莱姆','圣石洞穴')),
            ('关卡／圣石目标','野猪洞穴',('野猪洞穴','哥布林遗迹','史莱姆王国',*STONE_STAGES)),
            ('难度','极难',HUNT_DIFFICULTIES),('次数','1',None)]):
            ttk.Label(row, text=label).grid(row=0, column=i, sticky='w')
            var = tk.StringVar(value=value)
            widget = ttk.Combobox(row,textvariable=var,values=choices,width=19) if choices else ttk.Entry(row,textvariable=var,width=10)
            if i in (0,2):
                widget.configure(state='readonly')
            widget.grid(row=1,column=i,padx=(0,8))
            variables.append(var)
            widgets.append(widget)
        free_only = tk.BooleanVar(value=True)
        runs_mode=tk.StringVar(value='用完当前免费体力')
        ttk.Label(row,text='执行方式').grid(row=0,column=4,sticky='w')
        mode_widget=ttk.Combobox(row,textvariable=runs_mode,values=('指定次数','用完当前免费体力'),state='readonly',width=18)
        mode_widget.grid(row=1,column=4)
        lowest = tk.BooleanVar(value=False)
        free_widget=ttk.Checkbutton(frame,text='这条计划只消耗免费体力',variable=free_only)
        free_widget.pack(anchor='w')
        def mode_changed(_=None):
            sweep=runs_mode.get()=='用完当前免费体力'
            if sweep:free_only.set(True)
            widgets[3].configure(state='disabled' if sweep else 'normal')
            free_widget.configure(state='disabled' if sweep else 'normal')
        mode_widget.bind('<<ComboboxSelected>>',mode_changed)
        mode_changed()
        ttk.Label(frame,text='圣石目标可直接选“自动选择库存最少”，也可指定火／水／风／光／暗。').pack(anchor='w',pady=6)
        inventory_preview=tk.StringVar()
        ttk.Label(frame,textvariable=inventory_preview,wraplength=870).pack(anchor='w',pady=3)
        def refresh_inventory():
            from hunting_resources import stone_inventory_text
            inventory_preview.set(stone_inventory_text(self.path.parent.parent))
            self.plan_inventory_job=window.after(1000,refresh_inventory)
        refresh_inventory()
        def cancel_inventory(event):
            if event.widget is window:
                try:window.after_cancel(self.plan_inventory_job)
                except (tk.TclError,AttributeError):pass
        window.bind('<Destroy>',cancel_inventory,add='+')
        ttk.Label(frame,text='每行独立设置；指定0次不执行。自动最少每轮重读五项库存，本轮选定后保持目标，未决回执不重发。').pack(anchor='w',pady=6)
        ttk.Label(frame,text='按列表顺序执行；米饭与火炬均读游戏余额。关闭“只用免费”可使用已持有的额外体力，不购买。').pack(anchor='w',pady=4)
        ttk.Label(frame,text='史莱姆、哥布林和狩猎场共享米饭；“用完免费”按顺序刷取，后面的计划只用剩余量。').pack(anchor='w',pady=3)

        auto_target='自动选择库存最少'
        def sync_target(_=None, category_changed=False):
            category = variables[0].get()
            options = (*HUNT_TARGETS[category],auto_target) if category=='圣石洞穴' else HUNT_TARGETS[category]
            if category != '圣石洞穴': lowest.set(False)
            widgets[1].configure(values=options, state='normal' if category == '狩猎场' else 'readonly')
            if category_changed or (category == '狩猎场' and variables[1].get() in (*STONE_STAGES,'哥布林遗迹','史莱姆王国',auto_target)):
                lowest.set(False)
                variables[1].set(options[0])
            elif category=='圣石洞穴' and lowest.get():
                variables[1].set(auto_target)
            elif category != '狩猎场' and variables[1].get() not in options:
                variables[1].set(options[0])
            elif not variables[1].get():variables[1].set(options[0])
        widgets[0].bind('<<ComboboxSelected>>', lambda event:sync_target(category_changed=True))
        def target_changed(_=None):
            lowest.set(variables[0].get()=='圣石洞穴' and variables[1].get()==auto_target)
            sync_target()
        widgets[1].bind('<<ComboboxSelected>>', target_changed)
        sync_target()

        def add(edit=False):
            category, stage, difficulty, runs = [v.get().strip() for v in variables]
            sweep=runs_mode.get()=='用完当前免费体力'
            if sweep:runs='1'
            if not runs.isdigit() or not difficulty or (not lowest.get() and not stage):
                messagebox.showerror('计划未保存', '请填写关卡、难度和非负整数次数。', parent=window)
                return
            if lowest.get() and category!='圣石洞穴':
                messagebox.showerror('计划未保存','“库存最少”仅用于圣石洞穴。',parent=window)
                return
            selection = tree.selection()
            if edit and not selection:
                return
            previous = plans[selection[0]] if edit else None
            plan = {'id':previous['id'] if previous else 'extra_'+uuid.uuid4().hex,
                    'category':category,'stage':None if lowest.get() else stage,
                    'difficulty':difficulty,'runs':int(runs),'free_only':free_only.get(),
                    'target_mode':'lowest_stock' if lowest.get() else 'specified'}
            if sweep:plan.update(runs_mode='all_free',free_only=True)
            candidate=copy.deepcopy(self.config)
            candidate['hunting']['extra_plans']=[plan]
            try:validate_config(candidate)
            except ValueError as error:
                messagebox.showerror('计划未保存',str(error),parent=window)
                return
            position = tree.index(previous['id']) if previous else 'end'
            if previous and hunt_target_key(previous)!=hunt_target_key(plan):
                plan['id']='extra_'+uuid.uuid4().hex
                del plans[previous['id']]
                tree.delete(previous['id'])
            plans[plan['id']]=plan
            if tree.exists(plan['id']):
                tree.item(plan['id'],values=display(plan))
            else:
                tree.insert('', position, iid=plan['id'], values=display(plan))
            tree.selection_set(plan['id'])

        def select(_=None):
            selected = tree.selection()
            if not selected:
                return
            plan = plans[selected[0]]
            for var,key in zip(variables,('category','stage','difficulty','runs')):
                var.set('' if plan.get(key) is None else str(plan[key]))
            free_only.set(hunt_free_only(self.config['hunting'],plan))
            lowest.set(plan.get('target_mode')=='lowest_stock')
            runs_mode.set('用完当前免费体力' if plan.get('runs_mode')=='all_free' else '指定次数')
            mode_changed()
            sync_target()

        def delete():
            for item in tree.selection():
                del plans[item]
                tree.delete(item)

        def confirm():
            self.config['hunting']['extra_plans'] = [copy.deepcopy(plans[i]) for i in tree.get_children()]
            window.destroy()

        def move(offset):
            selected=tree.selection()
            if selected:
                tree.move(selected[0], '', max(0,tree.index(selected[0])+offset))

        controls = ttk.Frame(frame)
        controls.pack(fill='x')
        ttk.Button(controls, text='添加', command=add).pack(side='left')
        ttk.Button(controls,text='保存所选行修改',command=lambda:add(True)).pack(side='left',padx=8)
        ttk.Button(controls, text='删除选中项', command=delete).pack(side='left')
        ttk.Button(controls, text='上移', command=lambda:move(-1)).pack(side='left',padx=6)
        ttk.Button(controls, text='下移', command=lambda:move(1)).pack(side='left')
        def auto_stone_preset():
            variables[0].set('圣石洞穴');variables[1].set(auto_target);lowest.set(True)
            runs_mode.set('用完当前免费体力');mode_changed();sync_target()
        ttk.Button(controls,text='自动圣石预设',command=auto_stone_preset).pack(side='left',padx=8)
        ttk.Button(controls, text='保留计划并返回', command=confirm).pack(side='right')
        tree.bind('<<TreeviewSelect>>',select)
        self.plan_editor={'window':window,'tree':tree,'variables':variables,'free_only':free_only,
                          'lowest':lowest,'add':add,'select':select,'confirm':confirm,
                          'widgets':widgets,'sync_target':sync_target,'move':move}
        self.plan_editor['target_changed']=target_changed
        self.plan_editor.update(runs_mode=runs_mode,mode_changed=mode_changed,inventory_preview=inventory_preview,auto_stone_preset=auto_stone_preset)

    def collect(self):
        c, f = copy.deepcopy(self.config), self.fields
        c['cache_date'] = f['cache_date'].get().strip()
        h = c['hunting']
        h['enabled'] = f['hunt_enabled'].get()
        h['daily_task_status'] = 'unknown'  # Status is observed from the task page, never entered here.
        h['daily_target'] = {'category':'狩猎场', 'stage':'野猪洞穴',
                             'difficulty':f['hunt_difficulty'].get().strip(),'free_only':True}
        g = c['goddess']
        g['enabled'], g['user_positioned_near_statue'] = f['goddess_enabled'].get(), f['positioned'].get()
        g['actions'] = [key for key in ('check_in','daily_reward') if f[key].get()]
        m = c['mirror']
        m['enabled'], m['free_only'], m['auto_skip'] = f['mirror_enabled'].get(), f['mirror_free'].get(), f['mirror_skip'].get()
        for key, field in [('max_battles','mirror_battles'),('resource_budget','mirror_budget'),('resource_multiplier','mirror_multiplier')]:
            m[key] = int(f[field].get())
        return validate_config(c)

    def save(self):
        try:
            config = self.collect()
            from game_calendar import check_execution_day
            check_execution_day(config['cache_date'])
            next_hash=hashlib.sha256(json.dumps(config,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()
            for ledger_path in (ROOT/'runtime').glob('*.json'):
                data=json.loads(ledger_path.read_text(encoding='utf-8'))
                pending=data.get('pending') or {}
                if (data.get('cache_date')==config['cache_date']
                        and pending.get('config_hash') and pending['config_hash']!=next_hash):
                    raise RuntimeError('有尚未核对的日常动作，请先核对结果，再更改设置。')
            if hashlib.sha256(self.path.read_bytes()).digest() != hashlib.sha256(self.original).digest():
                raise RuntimeError('设置文件已被其他窗口修改，请重新打开此窗口。')
            encoded = (json.dumps(config, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
            # A date change applies to every module, with the same control lock
            # as execution. Keep both config files coherent or restore originals.
            from pending_daily import exclusive_ledger
            from local_cache import load_local_config, save
            with exclusive_ledger(self.path.parent.parent / 'runtime/game_control'):
                check_execution_day(config['cache_date'])
                if hashlib.sha256(self.path.read_bytes()).digest() != hashlib.sha256(self.original).digest():
                    raise RuntimeError('设置文件已被其他窗口修改，请重新打开此窗口。')
                event_path = self.path.with_name('event_and_rewards.json')
                event_before = event_path.read_bytes() if event_path.exists() else None
                if event_before != self.event_original:
                    raise RuntimeError('公会或活动设置已被其他窗口修改，请重新打开设置。')
                event_config = load_local_config(event_path) if event_before else None
                if event_config:
                    event_config = self.collect_event(event_config)
                elif self.fields['guild_enabled'].get() or self.fields['event_enabled'].get():
                    raise RuntimeError('缺少公会配置，请完整解压新版程序。')
                old_date = self.config['cache_date']
                if old_date != config['cache_date']:
                    for path in (self.path.parent.parent / 'runtime').glob('*.json'):
                        if json.loads(path.read_text(encoding='utf-8')).get('pending'):
                            raise RuntimeError('有尚未核对的操作，暂不能更换缓存日期。')
                try:
                    if event_config:
                        event_config['cache_date'] = config['cache_date']
                        save(event_path, event_config)
                    temp = self.path.with_suffix('.json.tmp')
                    temp.write_bytes(encoded); os.replace(temp, self.path)
                except OSError:
                    if event_before:
                        event_path.write_bytes(event_before)
                    raise
            self.original, self.config = encoded, config
            self.event_original = event_path.read_bytes() if event_path.exists() else None
            self.event_config = event_config
            self.saved.set('已保存 · 未启动游戏操作')
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            messagebox.showerror('设置未保存', str(error), parent=self.root)


def main():
    root = tk.Tk()
    SettingsWindow(root, ROOT / 'config/daily_tasks.json')
    root.mainloop()


if __name__ == '__main__':
    main()
