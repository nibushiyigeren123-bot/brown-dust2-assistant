"""通行证逐本直接领取一次；只记录输入完成，不核实奖励到账。"""
from reward_overlay_vision import reward_dismiss_point
from frame_store import next_path, report_write, persist_references
import argparse
import json
import time
from pathlib import Path
from local_cache import cache_path, load_local_config
import cv2
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
        # [隔离范围] 角色与两本既有通行证分别记账，pending不得相互覆盖。
        self.module_id = 'character_pass' if getattr(args,'scope','ordinary')=='character' else 'pass_rewards'
        self.report.update(action_groups=[], scope=self.module_id,
                           action_policy='direct_once', reward_verified=False)

    def capture(self):
        """[导航读取] 只等待页面和通行证身份，不以按钮亮暗或任务进度阻挡输入。"""
        # [阶段接线] 同一识别器跨普通/角色阶段复用；每次抓帧及焦点重读都按当前范围更新需求。
        self.vision.require_character_entry = (self.module_id == 'character_pass' and
            self.config['rewards']['character_pass_enabled'] is True)
        from runtime_policy import network_wait_seconds, wait_observed
        target=getattr(self,'transition_target',None)
        def matches(result):
            obs=result[0]
            if target and (obs['state'] not in target['states'] or
                           (target.get('pass_id') and obs.get('pass_id')!=target['pass_id'])):
                return False
            if obs['state'] in ('pass_tasks','pass_levels'):
                return obs.get('pass_id') in ('special','season','character')
            return obs['state'] in ('main_menu','reward_overlay')
        try:
            return wait_observed(self._capture_once,matches,network_wait_seconds(self.data_root),
                                 stop=check_requested_stop)
        finally:
            self.transition_target=None

    def expect_after(self,label):
        # [角色切换] 沿用父类等待，补足独立角色身份，不能被特别赛季页面提前放行。
        super().expect_after(label)
        if label=='select_pass_character':
            self.transition_target={'states':('pass_tasks','pass_levels'),'pass_id':'character'}

    def enter_pass(self, identity, tab=None):
        for _ in range(5):
            located = self.capture()
            obs = located[0]
            state = obs['state']
            if state == 'main_menu':
                self.click([1108, 174], state, 'open_pass')
            elif state in ('pass_tasks', 'pass_levels'):
                if obs['pass_id'] != identity:
                    # [入口] 角色只点本帧固定后缀定位的卡片，绝不按角色姓名或旧人物模板点击。
                    point=(obs.get('character_entry') or {}).get('point') if identity=='character' else [280,215 if identity=='special' else 283]
                    if not point:raise RuntimeError('未读清角色通行证固定后缀入口，未点击。')
                    self.click(point,state,'select_pass_'+identity)
                elif tab is not None and obs['tab'] != tab:
                    self.click([1063, 452 if tab == 'tasks' else 267], state, 'select_pass_' + tab)
                else:
                    return located
            elif state == 'reward_overlay':
                self.dismiss_reward('dismiss_pass_reward')
            else:
                raise RuntimeError('当前未识别到本期通行证或主页。')
        raise RuntimeError('通行证切页未核实。')

    def submit_direct(self, identity, tab, source=None):
        """[直接输入] 复用本次已定位页面，仅做窗口安全输入；实际输入回执不是奖励证明。"""
        if self.ledger.get('pending'):
            raise RuntimeError('通行证存在未决操作，保留原记录，不重放。')
        state = 'pass_' + tab
        located = source if source is not None else self.capture()
        obs, geo, _, path = located
        if (obs['state'] not in ('pass_tasks', 'pass_levels') or obs.get('pass_id') != identity
                or (source is None and obs['state'] != state)):
            raise RuntimeError('直接领取前通行证页面已改变，未提交。')
        # [坐标] 使用既有1280×720客户区规范点，交给窗口接口检查遮挡、停止键和鼠标移动。
        point = [966, 511]
        owned = {'module': self.module_id, 'type': 'direct_pass_' + identity + '_' + tab,
                 'point': point, 'state': state, 'pass_id': identity, 'tab': tab,
                 'evidence': str(path), 'source_state': obs['state'], 'reward_verified': False}
        self.ledger['pending'] = owned
        save(self.path, self.ledger)
        submission = {}
        # [异常边界] 输入失败、回执不明或落盘失败均保留本次pending，绝不自动补点。
        event = self.window.click_client(
            [point[0] * geo['width'] / 1280, point[1] * geo['height'] / 720],
            geo, press_ms=250, submission_state=submission)
        self._persist_input_receipt(owned, event, submission)
        if (submission.get('protocol') != 'game_window_click_v1'
                or submission.get('phase') != 'submitted'
                or not isinstance(event, dict) or event.get('input_kind') != 'left_click'):
            raise RuntimeError('通行证输入回执不完整，保留pending，不重放。')
        # [结算动作] 仅结算本次已正常返回的输入；完整意图与回执留在events，不生成到账claims。
        fresh = json.loads(self.path.read_text(encoding='utf-8'))
        if fresh.get('pending') != self.ledger.get('pending'):
            raise RuntimeError('直接领取回执身份已改变，保留记录。')
        receipt = {'type': 'direct_pass_attempt', 'intent': owned, 'input_receipt': event,
                   'submission_state': dict(submission), 'action_completed': True,
                   'reward_verified': False}
        fresh.setdefault('events', []).append(receipt)
        fresh['pending'] = None
        save(self.path, fresh)
        self.ledger.clear()
        self.ledger.update(fresh)
        self.report['actions'].append({**owned, **event, 'action_completed': True})
        self.transition_target = None
        time.sleep(.25)

    def scan_group(self, identity, tab):
        """[逐本动作] 无条件点一次记事板/等级页，再点一次全部获得，不扫描列表或核对奖励。"""
        # [旧未决] 在导航和记事板之前拒绝，不能先点页签再发现旧pending。
        if self.ledger.get('pending'):
            raise RuntimeError('通行证存在未决操作，保留原记录，不重放。')
        located = self.enter_pass(identity)
        obs, geo, _, path = located
        if obs['state'] not in ('pass_tasks', 'pass_levels') or obs.get('pass_id') != identity:
            raise RuntimeError('未定位预期通行证，未点击记事板或等级页。')
        # [页签动作] 已定位后直接点一次；GameWindow继续检查窗口、焦点、遮挡、停止键和鼠标。
        point = [1063, 452 if tab == 'tasks' else 267]
        submission = {}
        event = self.window.click_client(
            [point[0] * geo['width'] / 1280, point[1] * geo['height'] / 720],
            geo, press_ms=250, submission_state=submission)
        if (submission.get('protocol') != 'game_window_click_v1' or submission.get('phase') != 'submitted'
                or not isinstance(event, dict) or event.get('input_kind') != 'left_click'):
            raise RuntimeError('通行证页签输入回执不完整，未提交全部获得。')
        self.report['actions'].append({'type': 'select_pass_' + tab, 'point': point,
            'pass_id': identity, 'evidence': str(path), **event, 'submission_state': dict(submission)})
        # [顺序] 正常页签输入后固定短等，再复用同一定位证据；不重复OCR确认任务页。
        time.sleep(.5)
        self.submit_direct(identity, tab, source=located)
        # [可选弹窗] 只读取一帧判断是否需要关闭；没有弹窗不等待其出现。
        observed = self._capture_once()
        if observed[0]['state'] == 'reward_overlay':
            self.dismiss_reward('dismiss_pass_reward')
        self.report['action_groups'].append(identity + ':' + tab)
        self.ledger.update(action_policy='direct_once', reward_verified=False,
            action_groups=[group for group in self.report['action_groups']
                if group.startswith('character:') == (self.module_id == 'character_pass')])
        save(self.path, self.ledger)

    def execute(self):
        """[串联动作] 按设置执行任务/等级一次领取及可选角色阶段；完成位仅表示动作完成。"""
        initial = (self.module_id, self.ledger, self.path)
        stages = [initial]
        try:
            if self.module_id == 'character_pass' and not self.config['rewards']['character_pass_enabled']:
                self.report.update(skipped=True, skip_reason='当期角色通行证在设置中关闭。')
                return
            if self.module_id == 'pass_rewards' and self.config['rewards']['character_pass_enabled']:
                character_path = cache_path(self.data_root, self.config, 'character_pass')
                character = json.loads(character_path.read_text(encoding='utf-8')) if character_path.exists() else {
                    'cache_date': self.config['cache_date'], 'pending': None, 'events': []}
                stages.append(('character_pass', character, character_path))
            # [提交前检查] 两份账本先全部检查，任何未决都不得清空、重放或先领普通奖励。
            for _, ledger, _ in stages:
                if ledger.get('pending'):
                    raise RuntimeError('通行证存在未决领取，须核对原回执，不重放。')
                if ledger.get('cache_date') != self.config['cache_date']:
                    raise RuntimeError('通行证账本日期不一致，保留原记录。')
            self.report['character_pass_enabled'] = self.config['rewards']['character_pass_enabled']
            for _, ledger, path in stages:
                ledger.update(completed=False, action_groups=[], action_policy='direct_once', reward_verified=False)
                save(path, ledger)
            # [阶段切换] 保持同一窗口和识别器，切账本后直接从通行证页进入角色页。
            for module_id, ledger, path in stages:
                self.module_id, self.ledger, self.path = module_id, ledger, path
                identities = ('character',) if module_id == 'character_pass' else self.config['rewards']['pass_order']
                for identity in identities:
                    for tab in ('tasks', 'levels'):
                        if self.config['rewards']['pass_' + tab]:
                            self.scan_group(identity, tab)
            obs, _, _, _ = self.capture()
            # [返回导航] 延后出现的奖励层也可关闭；仅为返回主页，不作为到账证明。
            if obs['state'] == 'reward_overlay':
                obs, _, _, _ = self.dismiss_reward('dismiss_pass_reward')
            if obs['state'] in ('pass_tasks', 'pass_levels'):
                self.click([113, 32], obs['state'], 'return_to_main_menu')
                obs, _, _, _ = self.capture()
            if obs['state'] != 'main_menu':
                raise RuntimeError('通行证返回主页未核实。')
            # [整体结果] 普通或角色失败时两份完成位均为False；只在统一返回后置True。
            for _, ledger, path in stages:
                if ledger.get('pending'):
                    raise RuntimeError('通行证仍有未决输入，保留记录，不标记动作完成。')
                ledger.update(completed=True, action_completed=True, reward_verified=False)
                save(path, ledger)
            self.report.update(completed=True, action_completed=True, returned_to_main_menu=True)
        except Exception as error:
            self.report['stopped'] = str(error)
            raise
        finally:
            self.report['pending'] = self.ledger.get('pending')
            save(self.args.output / 'run.json', self.report)
            self.module_id, self.ledger, self.path = initial


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--scope',choices=('ordinary','character'),default='ordinary')
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
        # [只读诊断] 角色scope对保存截图启用同一入口恢复路径，不产生游戏输入。
        obs = PassRewardVision(require_character_entry=(args.scope=='character')).inspect_file(path, args.output / 'ocr')
        save(args.output / 'run.json', {'execute': False, 'completed': False, 'actions': [], 'observations': [{'image': str(path), 'observation': obs}]})
        return
    data_root = args.data_root.resolve(strict=True)
    config = load_config(data_root / 'config/event_and_rewards.json')
    # [旧入口兼容] 关闭角色开关时不创建游戏窗口，不允许旧workflow绕过设置。
    if args.scope == 'character' and not config['rewards']['character_pass_enabled']:
        save(args.output / 'run.json', {'execute': True, 'completed': False, 'skipped': True,
             'cache_date': config['cache_date'], 'scope': 'character_pass', 'actions': [],
             'observations': [], 'skip_reason': '当期角色通行证在设置中关闭。'})
        return
    daily = load_daily(data_root / 'config/daily_tasks.json')
    if any(config[key] != daily[key] for key in ('cache_date',)):
        raise RuntimeError('通行证缓存日期与日常配置不一致。')
    if any(value not in ('special', 'season') for value in config['rewards']['pass_order']):
        raise ValueError('当前适配仅覆盖已留证的两本通行证')
    import hashlib
    path = cache_path(data_root, config, 'character_pass' if args.scope=='character' else 'pass_rewards')
    with exclusive_ledger(data_root / 'runtime/game_control'):
        guard_foreign_pending(data_root / 'runtime', path, module='rewards')
        ledger = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'cache_date': config['cache_date'], 'pending': None, 'events': []}
        if ledger.get('pending'):
            raise RuntimeError('通行证存在未决领取，须核对原回执，不重放。')
        run = PassRun(args, config, ledger, path)
        run.data_root = data_root
        run.execute()


if __name__ == '__main__':
    main()
