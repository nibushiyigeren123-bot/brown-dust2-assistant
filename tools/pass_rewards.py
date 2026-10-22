"""Two current passes: fresh task and level scans with pending-aware claims."""
from reward_overlay_vision import reward_dismiss_point
from frame_store import next_path, report_write, persist_references
import argparse
import json
import time
from pathlib import Path
from local_cache import cache_path, load_local_config
import cv2
import win32api
import win32con
import win32gui
from daily_tasks import guard_foreign_pending, load_config as load_daily
from event_reward_rules import load_config
from game_window import GameWindow, check_requested_stop
from pass_reward_vision import PassRewardVision
from pending_daily import exclusive_ledger
from project_paths import ROOT
from task_rewards import TaskRewardRun, save


class PassRun(TaskRewardRun):
    def __init__(self, args, config, ledger, path):
        super().__init__(args, config, ledger, path)
        self.vision = PassRewardVision()
        self.module_id = 'pass_rewards'
        self.report.update(checked_groups=[])

    def scroll_pass(self, amount):
        obs, geo, _, _ = self.capture()
        if obs['state'] not in ('pass_tasks', 'pass_levels'):
            raise RuntimeError('通行证滚动前页面未知，已停止。')
        # Empty artwork focus excludes the pass/level purchase buttons.
        self.window.focus_client(geo)
        point = win32gui.ClientToScreen(self.window.hwnd, (round(850 * geo['width'] / 1280), round(365 * geo['height'] / 720)))
        previous = win32api.GetCursorPos()
        try:
            win32api.SetCursorPos(point)
            for _ in range(abs(amount)):
                check_requested_stop()
                if self.window.geometry() != geo or win32gui.GetForegroundWindow() != self.window.hwnd:
                    raise RuntimeError('通行证滚动时游戏窗口已改变。')
                if win32gui.GetAncestor(win32gui.WindowFromPoint(point), win32con.GA_ROOT) != self.window.hwnd:
                    raise RuntimeError('通行证列表被遮挡。')
                win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, 120 if amount > 0 else -120)
                time.sleep(.15)
        finally:
            win32api.SetCursorPos(previous)
        self.report['actions'].append({'type': 'scroll_pass', 'wheel': amount})
        time.sleep(.6)

    def enter_pass(self, identity, tab):
        for _ in range(5):
            obs, _, _, _ = self.capture()
            state = obs['state']
            if state == 'main_menu':
                self.click([1108, 174], state, 'open_pass')
            elif state in ('pass_tasks', 'pass_levels'):
                if obs['pass_id'] != identity:
                    self.click([280, 215 if identity == 'special' else 283], state, 'select_pass_' + identity)
                elif obs['tab'] != tab:
                    self.click([1063, 452 if tab == 'tasks' else 267], state, 'select_pass_' + tab)
                else:
                    return
            else:
                raise RuntimeError('当前未识别到本期通行证或主页。')
        raise RuntimeError('通行证切页未核实。')

    def scan_group(self, identity, tab):
        self.enter_pass(identity, tab)
        obs, _, _, _ = self.capture()
        if (obs['state'] != 'pass_' + tab or obs.get('pass_id') != identity
                or type(obs.get('claimable')) is not bool):
            raise RuntimeError('通行证身份、页签或全部获得按钮未读清。')
        reward_path = None
        post = None
        if obs['claimable']:
            self.click([966, 511], obs['state'], 'claim_pass_' + identity + '_' + tab, claim=True)
            receipt, _, _, reward_path = self.capture()
            if receipt['state'] != 'reward_overlay':
                raise RuntimeError('通行证已提交领取但奖励未核实，保留pending，禁止补点。')
            post = self.dismiss_reward('dismiss_pass_reward')
            if (post[0]['state'] != 'pass_' + tab or post[0].get('pass_id') != identity
                    or post[0].get('claimable') is not False):
                raise RuntimeError('通行证领取结果未确认，保留pending。')
        if tab == 'tasks':
            # Remaining tasks move above completed tasks. One top view suffices;
            # never traverse completed rows or compare both list endpoints.
            self.scroll_pass(12)
        final = self.confirm_no_rewards('pass_' + tab, identity,
                                        first=post if tab == 'levels' else None)
        if reward_path is not None:
            event = {'pass_id': identity, 'tab': tab, 'reward_image': str(reward_path)}
            self.ledger.setdefault('events', []).append(event)
            self.report['claims'].append(event)
            self.ledger['pending'] = None
            save(self.path, self.ledger)
        if tab == 'tasks':
            self.report['incomplete_visible'].extend(
                {'pass_id': identity, **row} for row in final.get('incomplete_visible', []))
        self.report.setdefault('coverage_proof', {})[identity + ':' + tab] = {
            'scope': 'available_rewards', 'basis': 'global_claim_all', 'claimable': False,
            'task_view': 'top_only' if tab == 'tasks' else None}
        self.report['checked_groups'].append(identity + ':' + tab)
        self.ledger['checked_groups'] = self.report['checked_groups'][:]
        save(self.path, self.ledger)

    def execute(self):
        try:
            for identity in self.config['rewards']['pass_order']:
                for tab in ('tasks', 'levels'):
                    if self.config['rewards']['pass_' + tab]:
                        self.scan_group(identity, tab)
            obs, _, _, _ = self.capture()
            if obs['state'] in ('pass_tasks', 'pass_levels'):
                self.click([113, 32], obs['state'], 'return_to_main_menu')
                obs, _, _, _ = self.capture()
            if obs['state'] != 'main_menu':
                raise RuntimeError('通行证返回主页未核实。')
            self.ledger.update(completed=True, pending=None)
            save(self.path, self.ledger)
            self.report.update(completed=True, returned_to_main_menu=True)
        except Exception as error:
            self.report['stopped'] = str(error)
            raise
        finally:
            self.report['pending'] = self.ledger.get('pending')
            save(self.args.output / 'run.json', self.report)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--image', type=Path)
    parser.add_argument('--data-root', type=Path, default=ROOT)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.image and args.execute:
        parser.error('保存的截图仅允许只读识别')
    if not args.execute:
        path = args.image
        if path is None:
            image, _ = GameWindow.find().capture()
            path = args.output / 'current.png'; path=next_path(path);image.save(path)
        obs = PassRewardVision().inspect_file(path, args.output / 'ocr')
        save(args.output / 'run.json', {'execute': False, 'completed': False, 'actions': [], 'observations': [{'image': str(path), 'observation': obs}]})
        return
    data_root = args.data_root.resolve(strict=True)
    config = load_config(data_root / 'config/event_and_rewards.json')
    daily = load_daily(data_root / 'config/daily_tasks.json')
    if any(config[key] != daily[key] for key in ('cache_date',)):
        raise RuntimeError('通行证缓存日期与日常配置不一致。')
    if any(value not in ('special', 'season') for value in config['rewards']['pass_order']):
        raise ValueError('当前适配仅覆盖已留证的两本通行证')
    import hashlib
    path = cache_path(data_root, config, 'pass_rewards')
    with exclusive_ledger(data_root / 'runtime/game_control'):
        guard_foreign_pending(data_root / 'runtime', path, module='rewards')
        ledger = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'cache_date': config['cache_date'], 'pending': None, 'events': []}
        if ledger.get('pending'):
            raise RuntimeError('通行证存在未决领取，须核对原回执，不重放。')
        ledger.update(completed=False, checked_groups=[])
        save(path, ledger)
        run = PassRun(args, config, ledger, path)
        run.data_root = data_root
        run.execute()


if __name__ == '__main__':
    main()
