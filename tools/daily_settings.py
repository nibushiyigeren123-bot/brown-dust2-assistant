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

from daily_tasks import load_config, validate_config, hunt_free_only, HUNT_DIFFICULTIES, STONE_STAGES, hunt_target_key
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
        self.fields = {}
        root.title('棕色尘埃2 · 日常设置')
        root.geometry('820x780')
        root.minsize(760, 740)
        from settings_theme import apply_settings_theme
        apply_settings_theme(root)
        frame = ttk.Frame(root, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='日常设置', font=('Microsoft YaHei UI', 18, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='保存后用于下一次运行。三个模块分别配置、逐项测试。').pack(anchor='w', pady=(4, 10))
        identity = ttk.Frame(frame)
        identity.pack(fill='x')
        self.entry(identity, 'cache_date', '数据缓存日期', self.config['cache_date'], 0)
        ttk.Label(frame, text='按日期读取本地进度，不读取账号。旧日期缓存不会自动重置；新日期需自行核对。').pack(anchor='w', pady=(5, 10))
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
        if key in ('hunt_difficulty','hunt_category','daily_status'):
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
        self.entry(parent, 'daily_status', '今天的狩猎任务', {'unknown':'待核对','not_done':'未完成','done':'已完成'}[c['daily_task_status']], 2,
                   ['待核对', '未完成', '已完成'])
        self.entry(parent, 'hunt_category', '固定一次的类别', c['daily_target']['category'], 3, ['狩猎场'])
        ttk.Label(parent,text='固定关卡：1 · 野猪洞穴（1次）').grid(row=4,column=0,columnspan=2,sticky='w',pady=6)
        self.entry(parent, 'hunt_difficulty', '固定一次的难度', c['daily_target']['difficulty'], 5, HUNT_DIFFICULTIES)
        self.entry(parent, 'rice_budget', '本轮米饭总额度', c['budgets']['rice'], 6)
        self.entry(parent, 'torch_budget', '本轮火把总额度', c['budgets']['torch'], 7)
        self.check(parent, 'hunt_free', '固定日常：只使用免费资源', hunt_free_only(c, c['daily_target']), 8)
        ttk.Label(parent, text='额度包含固定一次；空白表示尚未设置，0 表示不使用。').grid(row=9, column=0, columnspan=2, sticky='w')
        ttk.Button(parent, text='逐项设置额外狩猎（难度／次数／免费体力）', command=self.extra_plans).grid(row=10, column=0, columnspan=2, sticky='w', pady=10)

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

    def extra_plans(self):
        window = tk.Toplevel(self.root)
        from settings_theme import BG
        window.configure(bg=BG)
        window.title('额外狩猎计划')
        window.geometry('920x580')
        frame = ttk.Frame(window, padding=15)
        frame.pack(fill='both', expand=True)
        tree = ttk.Treeview(frame, style='Settings.Treeview', columns=('category','stage','difficulty','runs','free'), show='headings', height=7)
        for key, label in [('category','类别'), ('stage','目标／圣石选择'), ('difficulty','难度'), ('runs','次数'), ('free','只用免费体力')]:
            tree.heading(key, text=label)
            tree.column(key, width=130)
        tree.pack(fill='both', expand=True)
        plans = {plan['id']:copy.deepcopy(plan) for plan in self.config['hunting']['extra_plans']}
        def display(plan):
            return (plan['category'], '库存最少' if plan.get('target_mode')=='lowest_stock' else plan['stage'],
                    plan['difficulty'], plan['runs'], '是' if hunt_free_only(self.config['hunting'],plan) else '否')
        for plan in plans.values():
            tree.insert('', 'end', iid=plan['id'], values=display(plan))
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=12)
        variables = []
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
        free_only = tk.BooleanVar(value=True)
        lowest = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame,text='这条计划只消耗免费体力',variable=free_only).pack(anchor='w')
        ttk.Checkbutton(frame,text='圣石洞穴：对比右上角五种库存，选择最少的一种',variable=lowest).pack(anchor='w',pady=6)
        ttk.Label(frame,text='每行独立设置；0次表示本轮不执行。圣石“库存最少”会在本游戏日首次执行该计划时选择并记录目标。').pack(anchor='w',pady=6)

        def add(edit=False):
            category, stage, difficulty, runs = [v.get().strip() for v in variables]
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
            if previous and hunt_target_key(previous)!=hunt_target_key(plan):
                plan['id']='extra_'+uuid.uuid4().hex
                del plans[previous['id']]
                tree.delete(previous['id'])
            plans[plan['id']]=plan
            if tree.exists(plan['id']):
                tree.item(plan['id'],values=display(plan))
            else:
                tree.insert('', 'end', iid=plan['id'], values=display(plan))
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

        def delete():
            for item in tree.selection():
                del plans[item]
                tree.delete(item)

        def confirm():
            self.config['hunting']['extra_plans'] = [copy.deepcopy(plans[i]) for i in tree.get_children()]
            window.destroy()

        controls = ttk.Frame(frame)
        controls.pack(fill='x')
        ttk.Button(controls, text='添加', command=add).pack(side='left')
        ttk.Button(controls,text='保存所选行修改',command=lambda:add(True)).pack(side='left',padx=8)
        ttk.Button(controls, text='删除选中项', command=delete).pack(side='left')
        ttk.Button(controls, text='保留计划并返回', command=confirm).pack(side='right')
        tree.bind('<<TreeviewSelect>>',select)
        self.plan_editor={'window':window,'tree':tree,'variables':variables,'free_only':free_only,
                          'lowest':lowest,'add':add,'select':select,'confirm':confirm}

    def collect(self):
        c, f = copy.deepcopy(self.config), self.fields
        c['cache_date'] = f['cache_date'].get().strip()
        h = c['hunting']
        h['enabled'] = f['hunt_enabled'].get()
        h['daily_task_status'] = {'待核对':'unknown','未完成':'not_done','已完成':'done'}[f['daily_status'].get()]
        h['daily_target'] = {'category':'狩猎场', 'stage':'野猪洞穴',
                             'difficulty':f['hunt_difficulty'].get().strip(),'free_only':f['hunt_free'].get()}
        for resource in ('rice','torch'):
            value = f[resource+'_budget'].get().strip()
            h['budgets'][resource] = int(value) if value else None
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
                if hashlib.sha256(self.path.read_bytes()).digest() != hashlib.sha256(self.original).digest():
                    raise RuntimeError('设置文件已被其他窗口修改，请重新打开此窗口。')
                event_path = self.path.with_name('event_and_rewards.json')
                event_before = event_path.read_bytes() if event_path.exists() else None
                event_config = load_local_config(event_path) if event_before else None
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
            self.saved.set('已保存 · 未启动游戏操作')
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            messagebox.showerror('设置未保存', str(error), parent=self.root)


def main():
    root = tk.Tk()
    SettingsWindow(root, ROOT / 'config/daily_tasks.json')
    root.mainloop()


if __name__ == '__main__':
    main()
