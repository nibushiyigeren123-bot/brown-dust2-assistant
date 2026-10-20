"""Daily claims always include a fresh weekly check; never performs unfinished tasks."""
import argparse
import hashlib
import json
import time
from pathlib import Path
from local_cache import cache_path, load_local_config
import cv2
import win32api
import win32con
import win32gui

from daily_tasks import guard_foreign_pending
from game_window import GameWindow, check_requested_stop
from m00_recognize import ROOT, read_image
from pending_daily import exclusive_ledger
from task_reward_vision import TaskRewardVision


def save(path, data):
    from atomic_json import write_json
    write_json(path, data)


def ledger_path(data_root, config):
    return cache_path(data_root, config, 'daily_weekly_rewards')


def begin_sweep(ledger):
    if ledger.get('pending'):
        raise RuntimeError('领奖存在未决操作，须核对原奖励回执后再继续，不重放领取。')
    # Completion belongs to a previous sweep; later tasks may unlock weekly rewards.
    ledger.update(completed=False, checked_modes=[], pending=None)


class TaskRewardRun:
    def __init__(self, args, config, ledger, path):
        self.args, self.config, self.ledger, self.path = args, config, ledger, path
        self.data_root = getattr(args, 'data_root', ROOT)
        self.window, self.vision = GameWindow.find(), TaskRewardVision()
        self.sequence = 0
        self.report = {'execute': True, 'completed': False,
                       'cache_date': config['cache_date'], 'observations': [], 'actions': [],
                       'checked_modes': [], 'claims': [], 'incomplete_visible': []}

    def _capture_once(self):
        check_requested_stop()
        for attempt in range(3):
            try:
                image, geo = self.window.capture()
                break
            except RuntimeError as error:
                if 'capture is black' not in str(error) or attempt == 2:
                    raise
                time.sleep(.5)
        path = self.args.output / f'frame_{self.sequence:03d}.png'
        self.sequence += 1
        image.save(path)
        obs = self.vision.inspect_file(path, self.args.output / 'ocr')
        self.report['observations'].append({'image': str(path), 'observation': obs})
        print(json.dumps({'frame': path.name, 'state': obs['state'], 'claimable': obs.get('claimable')}, ensure_ascii=False), flush=True)
        save(self.args.output / 'run.json', self.report)
        return obs, geo, read_image(path), path

    def capture(self):
        from runtime_policy import network_wait_seconds, wait_observed
        target = getattr(self, 'transition_target', None)
        def matches(result):
            obs = result[0]
            if target:
                return obs['state'] in target['states'] and (not target.get('pass_id') or obs.get('pass_id') == target['pass_id'])
            return obs['state'] != 'unknown'
        def waiting(elapsed, timeout):
            self.report['waiting'] = {'elapsed_seconds': round(elapsed, 1), 'timeout_seconds': timeout,
                                      'message': f'等待网络或页面响应 {elapsed:.0f}/{timeout:g} 秒；不重复点击'}
            save(self.args.output / 'run.json', self.report)
        try:
            result = wait_observed(self._capture_once, matches, network_wait_seconds(getattr(self, 'data_root', ROOT)),
                                   on_wait=waiting, stop=check_requested_stop)
            self.transition_target = None
            return result
        finally:
            self.report.pop('waiting', None)

    def expect_after(self, label):
        states = None
        identity = None
        if label.startswith('claim_'):
            states = ('reward_overlay',)
        elif label.startswith('dismiss_'):
            states = ('daily_tasks', 'weekly_tasks', 'pass_tasks', 'pass_levels')
        elif label == 'open_daily_tasks' or label == 'select_daily':
            states = ('daily_tasks',)
        elif label == 'select_weekly':
            states = ('weekly_tasks',)
        elif label == 'open_pass':
            states = ('pass_tasks', 'pass_levels')
        elif label in ('select_pass_tasks', 'select_pass_levels'):
            states = ('pass_' + label.rsplit('_', 1)[1],)
        elif label in ('select_pass_special', 'select_pass_season'):
            states = ('pass_tasks', 'pass_levels'); identity = label.rsplit('_', 1)[1]
        elif label == 'return_to_main_menu':
            states = ('main_menu',)
        self.transition_target = {'states': states, 'pass_id': identity} if states else None

    def click(self, point, state, label, claim=False):
        obs, geo, _, path = self.capture()
        expected_pass = label[len('claim_pass_'):].split('_')[0] if label.startswith('claim_pass_') else None
        identity = obs.get('pass_id')
        if obs['state'] != state or (claim and obs.get('claimable') is not True) or (expected_pass and identity != expected_pass):
            raise RuntimeError('领奖输入前页面或可领取状态已改变，已停止。')
        self.window.focus_client(geo)
        obs, fresh_geo, _, path = self.capture()
        if fresh_geo != geo or obs['state'] != state or obs.get('pass_id') != identity or (claim and obs.get('claimable') is not True):
            raise RuntimeError('移焦后页面或可领取状态已改变，未提交。')
        intent = {'type': label, 'point': point, 'state': state, 'evidence': str(path)}
        self.report['pending_input'] = intent
        if claim:
            self.ledger['pending'] = {'module': getattr(self, 'module_id', 'task_rewards'), **intent}
            save(self.path, self.ledger)
        save(self.args.output / 'run.json', self.report)
        # The observed client occasionally ignores a short press; keep hover and press visible.
        position = win32gui.ClientToScreen(self.window.hwnd, (round(point[0] * geo['width'] / 1280), round(point[1] * geo['height'] / 720)))
        assert self.window.geometry() == geo
        assert win32gui.GetAncestor(win32gui.WindowFromPoint(position), win32con.GA_ROOT) == self.window.hwnd
        previous = win32api.GetCursorPos()
        try:
            win32api.SetCursorPos(position)
            time.sleep(.8)
            check_requested_stop()
            assert self.window.geometry() == geo and win32gui.GetForegroundWindow() == self.window.hwnd
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
            try:
                time.sleep(.5)
            finally:
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)
            time.sleep(.4)
        finally:
            win32api.SetCursorPos(previous)
        self.report['actions'].append(intent)
        self.report.pop('pending_input', None)
        self.expect_after(label)
        time.sleep(1)

    def scroll(self, mode, amount):
        obs, geo, _, _ = self.capture()
        if obs['state'] != mode + '_tasks':
            raise RuntimeError('滚动前未识别到预期的任务页。')
        self.window.focus_client(geo)
        point = win32gui.ClientToScreen(self.window.hwnd, (round(850 * geo['width'] / 1280), round(450 * geo['height'] / 720)))
        assert win32gui.GetAncestor(win32gui.WindowFromPoint(point), win32con.GA_ROOT) == self.window.hwnd
        previous = win32api.GetCursorPos()
        try:
            win32api.SetCursorPos(point)
            time.sleep(.3)
            for _ in range(abs(amount)):
                check_requested_stop()
                assert self.window.geometry() == geo and win32gui.GetForegroundWindow() == self.window.hwnd
                win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, 120 if amount > 0 else -120)
                time.sleep(.15)
        finally:
            win32api.SetCursorPos(previous)
        self.report['actions'].append({'type': 'scroll_tasks', 'mode': mode, 'wheel': amount})
        time.sleep(.6)

    def enter(self, mode):
        for _ in range(3):
            obs, _, _, _ = self.capture()
            if obs['state'] == mode + '_tasks':
                return
            if obs['state'] == 'main_menu':
                self.click([455, 655], 'main_menu', 'open_daily_tasks')
            elif obs['state'] in ('daily_tasks', 'weekly_tasks'):
                self.click([190, 100 if mode == 'daily' else 147], obs['state'], 'select_' + mode)
            else:
                raise RuntimeError('请从主页、每日或每周任务页启动；当前页面未识别。')
        raise RuntimeError('未能切换到' + mode + '任务页，未标记检查完成。')

    def check_mode(self, mode):
        self.enter(mode)
        self.scroll(mode, 6)
        obs, _, image, _ = self.capture()
        from reward_endpoints import RewardEndpoints
        endpoints = RewardEndpoints()
        if not endpoints.confirmed(image, mode + ':top'):
            self.report.setdefault('coverage_warnings',[]).append(mode+':顶部内容变化，任务覆盖未确认；继续核对全局全部获得按钮。')
        if obs.get('claimable') is None:
            raise RuntimeError('未读清全部获得按钮，未领取。')
        if obs['claimable']:
            self.click([1120, 673], mode + '_tasks', 'claim_' + mode, claim=True)
            for _ in range(4):
                receipt, _, _, reward_path = self.capture()
                if receipt['state'] == 'reward_overlay':
                    break
                time.sleep(.7)
            else:
                raise RuntimeError('领取已发出但未确认奖励页，保留未决记录，不重复领取。')
            self.click([760, 645], 'reward_overlay', 'dismiss_' + mode + '_reward')
            obs, _, image, _ = self.capture()
            if obs['state'] != mode + '_tasks' or obs.get('claimable') is not False:
                raise RuntimeError('奖励关闭后尚未确认领取完毕，保留未决记录。')
            event = {'mode': mode, 'reward_image': str(reward_path)}
            self.report['claims'].append(event)
            self.ledger.setdefault('events', []).append(event)
            self.ledger['pending'] = None
            save(self.path, self.ledger)
        last = cv2.resize(image, (1280, 720))[85:645, 385:1195]
        stable = 0
        for _ in range(10):
            self.report['incomplete_visible'].extend({'mode': mode, **r} for r in obs.get('incomplete_visible', []))
            self.scroll(mode, -3)
            obs, _, image, _ = self.capture()
            if obs['state'] != mode + '_tasks' or obs.get('claimable') is not False:
                raise RuntimeError('列表复查中页面或领取状态发生变化，请核对。')
            current = cv2.resize(image, (1280, 720))[85:645, 385:1195]
            difference = float(cv2.absdiff(current, last).mean())
            stable = stable + 1 if difference < .3 else 0
            last = current
            if stable >= 2:
                if endpoints.confirmed(image, mode + ':bottom'):
                    break
                self.report.setdefault('coverage_warnings',[]).append(mode+':底部覆盖未确认；不推断所有任务完成。')
                break
        else:
            self.report.setdefault('coverage_warnings',[]).append(mode+':列表覆盖达到观察上限；不推断所有任务完成。')
        # The global claim-all button covers available rewards independently of
        # list position. Task-list coverage is separate from reward collection.
        self.report['all_tasks_completed']=False
        self.report['checked_modes'].append(mode)
        self.ledger['checked_modes'] = self.report['checked_modes'][:]
        save(self.path, self.ledger)

    def execute(self):
        try:
            for mode in ('daily', 'weekly'):
                self.check_mode(mode)
            obs, _, _, _ = self.capture()
            for _ in range(3):
                if obs['state'] == 'main_menu':
                    break
                if obs['state'] not in ('daily_tasks', 'weekly_tasks'):
                    raise RuntimeError('收尾页面未知，尚未确认返回主页。')
                self.click([113, 32], obs['state'], 'return_to_main_menu')
                obs, _, _, _ = self.capture()
            if obs['state'] != 'main_menu':
                raise RuntimeError('尚未确认返回主页。')
            self.report.update(completed=True, returned_to_main_menu=True)
            self.ledger.update(completed=True, pending=None)
            save(self.path, self.ledger)
        except Exception as error:
            self.report['stopped'] = str(error)
            raise
        finally:
            self.report['pending'] = self.ledger.get('pending')
            save(self.args.output / 'run.json', self.report)


def reconcile_pending_claim(data_root, output):
    """Reconcile only idempotent daily/weekly claims from their current task page.

    Caller owns the game lock. Never handles currency consumption or another
    module's pending intent. Keep the original intent until a terminal UI check.
    """
    from game_calendar import game_day
    root, output = Path(data_root), Path(output)
    config = load_local_config(root / 'config/daily_tasks.json')
    path = ledger_path(root, config)
    if not path.exists():
        return None
    ledger = json.loads(path.read_text(encoding='utf-8'))
    original = ledger.get('pending')
    if not original:
        return None
    if (config.get('cache_date') != game_day().isoformat()
            or ledger.get('cache_date') != config['cache_date']
            or original.get('module') != 'task_rewards'
            or original.get('type') not in ('claim_daily', 'claim_weekly')):
        raise RuntimeError('领奖未决身份或日期不符，保留原记录。')
    mode = original['type'].split('_', 1)[1]
    if original.get('state') != mode + '_tasks':
        raise RuntimeError('领奖未决页面与领取类型不符，保留原记录。')
    guard_foreign_pending(root / 'runtime', path, module='task_rewards')
    output.mkdir(parents=True, exist_ok=True)
    run = TaskRewardRun(argparse.Namespace(output=output, data_root=root), config, ledger, path)
    run.enter(mode)
    first, _, _, _ = run.capture()
    second, _, _, current_path = run.capture()
    if (first['state'] != mode + '_tasks' or second['state'] != first['state']
            or type(second.get('claimable')) is not bool
            or first.get('claimable') != second.get('claimable')):
        raise RuntimeError('领奖恢复尚未连续确认任务页与可领取状态，保留未决。')
    recovery = {'original_intent': original, 'verified_task_image': str(current_path),
                'available_before': second['claimable'], 'status': 'checking'}
    ledger.setdefault('claim_recoveries', []).append(recovery)
    save(path, ledger)
    if second['claimable']:
        # Claim-all can grant only currently available rewards. Preserve both
        # the interrupted intent and this fresh intent if the network drops again.
        run.click([1120, 673], mode + '_tasks', 'claim_' + mode, claim=True)
        receipt, _, _, reward_path = run.capture()
        if receipt['state'] != 'reward_overlay':
            raise RuntimeError('恢复领取未确认奖励页，保留原记录和本次未决。')
        run.click([760, 645], 'reward_overlay', 'dismiss_' + mode + '_reward')
        after, _, _, after_path = run.capture()
        if after['state'] != mode + '_tasks' or after.get('claimable') is not False:
            raise RuntimeError('恢复领取后的任务页尚未确认领完，保留未决。')
        recovery.update(status='available_rewards_claimed', reward_image=str(reward_path),
                        terminal_image=str(after_path))
        ledger.setdefault('events', []).append({'mode': mode, 'reward_image': str(reward_path), 'recovered': True})
    else:
        # This verifies the terminal task state, not the lost historical receipt.
        recovery.update(status='no_available_rewards', terminal_image=str(current_path),
                        historical_award_receipt_verified=False)
    ledger['pending'] = None
    ledger['completed'] = False
    save(path, ledger)
    from common_navigation import ensure_main_menu
    home = ensure_main_menu(run.window, output / 'return_home', execute=True, timeout=60)
    if not home['completed']:
        raise RuntimeError('领奖已核对，但返回主菜单尚未确认。')
    save(output / 'recovery.json', recovery)
    return recovery


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--data-root', type=Path, default=ROOT)
    parser.add_argument('--image', type=Path)
    args = parser.parse_args()
    args.output = args.output.resolve(); args.output.mkdir(parents=True, exist_ok=True)
    if not args.execute:
        path = args.image
        if not path:
            image, _ = GameWindow.find().capture()
            path = args.output / 'readonly.png'; image.save(path)
        observation = TaskRewardVision().inspect_file(path, args.output / 'ocr')
        save(args.output / 'run.json', {'execute': False, 'completed': False,
             'actions': [], 'observations': [{'observation': observation}]})
        return
    if args.image:
        raise ValueError('Saved-image inspection cannot send input')
    config = load_local_config(args.data_root / 'config/daily_tasks.json')
    path = ledger_path(args.data_root, config)
    with exclusive_ledger(args.data_root / 'runtime/game_control'):
        guard_foreign_pending(args.data_root / 'runtime', path, module='task_rewards')
        reconcile_pending_claim(args.data_root, args.output / 'pending_recovery')
        ledger = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
            'schema_version': 1, 'cache_date': config['cache_date'], 'pending': None}
        begin_sweep(ledger)
        save(path, ledger)
        TaskRewardRun(args, config, ledger, path).execute()


if __name__ == '__main__':
    main()
