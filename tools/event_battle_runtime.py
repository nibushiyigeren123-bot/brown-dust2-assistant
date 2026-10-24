"""Own event input, double-frame submit checks and the shared AP journal."""
from frame_store import next_path, report_write, persist_references
import copy
import hashlib
import json
import time
import uuid
import os
from pathlib import Path

from common_navigation import ensure_main_menu
from daily_tasks import guard_foreign_pending
from local_cache import iter_cache_records
from event_battle_vision import EventBattleVision
from event_resources import retain_balance
from event_reward_rules import load_config, event_ledger_path
from game_calendar import game_day
from game_window import GameWindow, check_requested_stop
from pending_daily import exclusive_ledger
from runtime_policy import network_wait_seconds
from task_rewards import save


OBSERVATION_KEYS = ('state', 'event_id', 'mode', 'stage', 'unit_cost', 'quantity',
                    'total_cost', 'free_only', 'free_ap', 'free_ap_capacity', 'owned_extra_ap',
                    'cleared_stage_evidence')


def stable_observation(first, second):
    """[双帧] 核对身份与通关语义，排除匹配浮点分数，避免画面微动阻断稳定确认。"""
    def value(observation,key):
        value=observation.get(key)
        if key=='cleared_stage_evidence' and isinstance(value,dict):
            return {k:value.get(k) for k in ('kind','event_id','mode','stage')}
        return value
    return all(value(first,key) == value(second,key) for key in OBSERVATION_KEYS)



def checkpoint_frame(path, observation):
    """[证据] 先将关键原帧从内存封存，再记录磁盘实际字节哈希与观察，避免恢复时原图已经释放。"""
    persist_references(str(path))
    return {'image': str(path), 'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            'observation': copy.deepcopy(observation)}


def verify_result_checkpoint(pending, vision, output):
    """[恢复核对] 不扫描历史；逐张核对本pending引用原图、原输入、最终胜利和返回回执。"""
    checkpoint = pending.get('result_checkpoint') if isinstance(pending, dict) else None
    if not checkpoint:
        return None
    keys = ('event_id', 'mode', 'method', 'start_stage', 'quantity', 'cost', 'unit_cost',
            'free_ap', 'free_only', 'cache_date', 'op_id', 'input_receipt')
    receipt = checkpoint.get('return_receipt') or {}
    if (checkpoint.get('schema_version') != 1 or receipt.get('input_kind') != 'left_click'
            or not receipt.get('client_point') or not receipt.get('screen_point')
            or type(receipt.get('press_ms')) is not int or receipt['press_ms'] <= 0):
        raise RuntimeError('活动检查点版本或原返回输入回执不完整。')
    if (any(checkpoint.get(key) != pending.get(key) for key in keys)
            or pending.get('method') != 'continuous' or not checkpoint.get('return_receipt')):
        raise RuntimeError('活动检查点身份或原回执不符，保留pending。')
    frames = checkpoint.get('victory_frames')
    if not isinstance(frames, list) or len(frames) != 2 or frames[0]['image'] == frames[1]['image']:
        raise RuntimeError('活动最终胜利检查点缺少两张独立原帧。')
    # [实图] 提交原图确认活动和费用；胜利原图重新识别末关，两张一致才恢复只收尾权限。
    observed = []
    for index, frame in enumerate([checkpoint.get('submit_frame', {}), *frames]):
        path = Path(frame.get('image', ''))
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != frame.get('sha256'):
            raise RuntimeError('活动检查点原图不存在或哈希不符。')
        current = vision.inspect_file(path, Path(output) / f'checkpoint_{index}')
        if not stable_observation(frame.get('observation', {}), current):
            raise RuntimeError('活动检查点原图重识别与原观察不符。')
        observed.append(current)
    submit = observed[0]
    if (submit.get('state') != 'event_auto_dialog' or submit.get('event_id') != pending['event_id']
            or submit.get('mode') != pending['mode'] or submit.get('stage') != pending['start_stage']
            or submit.get('total_cost') != pending['cost'] or submit.get('free_only') != pending['free_only']
            or submit.get('free_ap') != pending['free_ap'] or submit.get('free_ap_capacity') != 5):
        raise RuntimeError('活动检查点提交原图与原批次不符。')
    if not stable_observation(observed[1], observed[2]):
        raise RuntimeError('活动检查点最终胜利双帧不稳定。')
    for victory in observed[1:]:
        if (victory.get('state') != 'event_victory' or victory.get('mode') != pending['mode']
                or victory.get('event_id') not in (None, pending['event_id'])
                or victory.get('stage') != pending['start_stage'] + pending['quantity'] - 1):
            raise RuntimeError('活动检查点不是原批次最终胜利。')
    return checkpoint

def wait_home_event(window, vision, root, output, report, expected_day):
    """Observe the automatic carousel; never click an unidentified card."""
    deadline = time.monotonic() + network_wait_seconds(root)
    previous = None
    index = 0
    while time.monotonic() < deadline:
        check_requested_stop()
        if game_day().isoformat() != expected_day:
            raise RuntimeError('等待活动入口时已换日，未进入活动。')
        image, geometry = window.capture()
        entry = output / f'home_carousel_{index:04d}.png'
        index += 1
        entry=next_path(entry);image.save(entry)
        observation = vision.inspect_file(entry, output / 'recognition')
        report['observations'].append({'image': str(entry), 'geometry': geometry, 'observation': observation})
        point = observation.get('points', {}).get('enter_event')
        evidence=observation.get('event_entry_evidence',{})
        signature = ('解锁',point,geometry) if (observation.get('state')=='main_menu' and point
            and evidence.get('recognition')=='fixed_carousel_unlock_keyword' and evidence.get('keyword')=='解锁') else None
        if signature is not None and signature == previous:
            report.pop('waiting', None)
            report['event_entry_images'] = [r['image'] for r in report['observations'][-2:]]
            persist_references(report['event_entry_images'])
            save(output / 'run.json', report)
            return observation
        previous = copy.deepcopy(signature)
        report['waiting'] = {'message': '等待主页右下活动卡字幕出现解锁',
                             'timeout_seconds': network_wait_seconds(root), 'frames': index}
        save(output / 'run.json', report)
        time.sleep(.5)
    raise RuntimeError('等待主页活动轮播超时，固定字幕区域未稳定读到解锁，未进入活动。')


def enter_and_verify_event_hub(window,vision,root,output,report,expected_day,expected_id):
    """[两阶段导航] 主页解锁双帧只准许进入；中心标题与控件双帧独立建立事件身份。"""
    observation=wait_home_event(window,vision,root,output,report,expected_day)
    original=report['observations'][-1];geometry=original['geometry']
    window.focus_client(geometry);check_requested_stop()
    if game_day().isoformat()!=expected_day:raise RuntimeError('进入活动前已换日。')
    image,fresh_geometry=window.capture();path=next_path(output/'event_entry_preclick.png');image.save(path)
    fresh=vision.inspect_file(path,output/'recognition')
    evidence=fresh.get('event_entry_evidence',{})
    if (fresh_geometry!=geometry or fresh.get('state')!='main_menu'
            or fresh.get('points',{}).get('enter_event')!=observation['points']['enter_event']
            or evidence.get('keyword')!='解锁' or evidence.get('recognition')!='fixed_carousel_unlock_keyword'):
        raise RuntimeError('活动入口在按下前改变，未执行点击。')
    report['event_entry_preclick']=checkpoint_frame(path,fresh);save(output/'run.json',report)
    point=fresh['points']['enter_event'];check_requested_stop()
    receipt=window.click_client([point[0]*geometry['width']/1280,point[1]*geometry['height']/720],geometry,press_ms=250)
    report['event_entry_receipt']=receipt
    report['returned_to_main_menu']=False
    report['actions'].append({'type':'enter_event','scope':'nonconsuming_entry_only','evidence':str(path),**receipt})
    save(output/'run.json',report)
    deadline=time.monotonic()+network_wait_seconds(root);previous=None;previous_frame=None;index=0
    while time.monotonic()<deadline:
        check_requested_stop()
        if game_day().isoformat()!=expected_day:raise RuntimeError('核实活动中心时已换日。')
        image,current_geometry=window.capture();path=next_path(output/f'event_hub_{index:03}.png');image.save(path);index+=1
        current=vision.inspect_file(path,output/'recognition')
        report['observations'].append({'image':str(path),'geometry':current_geometry,'observation':current})
        valid=(current.get('state')=='event_hub' and current.get('title_verified') is True
            and bool(current.get('event_id')) and all(current.get('points',{}).get(mode) for mode in ('normal','challenge')))
        signature=(current.get('event_id'),current_geometry) if valid else None
        if signature is not None and signature==previous:
            if expected_id not in ('auto_current','configure_current_event',current['event_id']):
                raise RuntimeError('活动中心实际身份与显式配置不符，未绑定账本。')
            verified={'observation':current,'entry_receipt':receipt,
                'hub_frames':[previous_frame,checkpoint_frame(path,current)]}
            report['verified_event_hub']=verified;save(output/'run.json',report)
            return verified
        previous=signature;previous_frame=checkpoint_frame(path,current) if valid else None
        save(output/'run.json',report);time.sleep(.25)
    raise RuntimeError('进入后未双帧核实活动中心身份，未绑定账本或提交战斗。')


class EventBattleRun:
    def __init__(self, args, config, ledger, ledger_path, window, report):
        from event_battle_flow import EventBattleController
        self.args, self.root, self.config = args, Path(args.data_root), config
        self.ledger, self.path, self.window, self.report = ledger, ledger_path, window, report
        event = copy.deepcopy(config['event'])
        event.setdefault('battle_method', 'quick' if event.get('quick_battle', {}).get('enabled') else 'continuous')
        # [恢复] 新检查点必须先由宿主核原图；旧pending不伪造胜利，不获得准备页收尾权限。
        self.vision, self.sequence = EventBattleVision(), 0
        verified = (verify_result_checkpoint(ledger.get('pending'), self.vision, args.output)
                    if getattr(args, 'reconcile_pending', False) else None)
        self.controller = (EventBattleController.resume_pending(event, ledger, verified)
                           if getattr(args, 'reconcile_pending', False)
                           else EventBattleController.from_verified_hub(event,ledger,report['verified_event_hub'])
                           if report.get('verified_event_hub') else EventBattleController(event, ledger))
        self.last_change = time.monotonic()
        self.started = self.last_change
        self.signature = None
        self.navigation_retry = None

    def retry_navigation(self, observation, geometry):
        """One recovery for an ignored, nonconsuming page-entry click only."""
        item = self.navigation_retry
        if not item or item['used'] or self.ledger.get('pending'):
            return False
        state, key = item['expected']
        if observation['state'] != state or observation.get('points', {}).get(key) != item['point']:
            # Seeing a transition retires this click's recovery budget forever,
            # even if a later visit happens to show the same page and point.
            self.navigation_retry = None
            return False
        if time.monotonic() - item['sent_at'] < 8:
            return False
        fresh, fresh_geometry, fresh_path = self.capture()
        if (fresh_geometry != geometry or not stable_observation(observation, fresh)
                or fresh.get('points', {}).get(key) != item['point']):
            return False
        item['used'] = True
        recovery = self.window.recover_input(fresh_geometry)
        self.report['actions'].append({'type': 'recover_event_navigation_input', **recovery})
        # Restoring the native window is followed by another full observation.
        verified, final_geometry, final_path = self.capture()
        if (final_geometry != fresh_geometry or not stable_observation(fresh, verified)
                or verified.get('points', {}).get(key) != item['point']):
            self.persist()
            return False
        point = item['point']
        receipt = self.window.click_client([point[0] * final_geometry['width'] / 1280,
                                           point[1] * final_geometry['height'] / 720], final_geometry, press_ms=250)
        self.report['actions'].append({'type': 'retry_nonconsuming_event_navigation',
            'original_action': item['action'], 'evidence': str(final_path), **receipt})
        self.last_change = time.monotonic()
        self.persist()
        return True

    def persist(self):
        self.report['pending'] = self.ledger.get('pending')
        self.report['progress'] = self.ledger
        save(self.args.output / 'run.json', self.report)
        if not self.args.prepare_only:
            save(self.path, self.ledger)

    def capture(self):
        check_requested_stop()
        image, geometry = self.window.capture()
        path = self.args.output / f'frame_{self.sequence:04d}.png'
        self.sequence += 1
        path=next_path(path);image.save(path)
        observation = self.vision.inspect_file(path, self.args.output / 'recognition')
        retain_balance(self.root, observation, self.config['cache_date'], self.config['event']['event_id'])
        self.report['observations'].append({'image': str(path), 'observation': observation})
        signature = tuple(str(observation.get(key)) for key in OBSERVATION_KEYS)
        if signature != self.signature:
            self.signature, self.last_change = signature, time.monotonic()
        self.persist()
        print(json.dumps({'state': observation['state'], 'mode': observation.get('mode'),
                          'stage': observation.get('stage'), 'cost': observation.get('total_cost'),
                          'free_ap': observation.get('free_ap')}, ensure_ascii=False), flush=True)
        return observation, geometry, path

    def preview_return(self, observation, geometry, path):
        point = observation.get('points', {}).get('cancel')
        if not point:
            raise RuntimeError('设置预览结束，但取消按钮未读清。')
        self.report['prepared_observation'] = observation
        self.report['prepared_image'] = str(path)
        self.report['actions'].append({'type': 'cancel_event_preview', **self.window.click_client(
            [point[0] * geometry['width'] / 1280, point[1] * geometry['height'] / 720], geometry, press_ms=250)})
        time.sleep(.5)
        home = ensure_main_menu(self.window, self.args.output / 'preview_return', execute=True,
                                timeout=network_wait_seconds(self.root))
        self.report.update(prepared=bool(home['completed']), returned_to_main_menu=bool(home['completed']),
                           completed=False, module_completed=False,
                           stopped='活动设置已核对，预览未提交战斗。' if home['completed'] else '设置已核对，返回主页尚未确认。')

    def execute(self):
        while True:
            absolute_limit = max(network_wait_seconds(self.root) * 4,
                                 min(7200, self.config['event']['runs'] * 180 + network_wait_seconds(self.root)))
            if time.monotonic() - self.started >= absolute_limit:
                raise RuntimeError('活动已到本次总等待上限，保存现场，不重新提交。')
            observation, geometry, path = self.capture()
            decision = self.controller.step(observation)
            self.report['decision'] = decision
            status = decision['status']
            if status == 'needs_review':
                raise RuntimeError(decision.get('reason') or '活动需要核对。')
            if status == 'finished':
                self.report.update(completed=True, returned_to_main_menu=True,
                                   skipped=bool(decision.get('skipped')),
                                   skip_reason=decision.get('reason') if decision.get('skipped') else None)
                self.ledger['completed'] = not decision.get('skipped')
                break
            if status == 'action':
                self.window.focus_client(geometry)
                time.sleep(.2)
                fresh, fresh_geometry, fresh_path = self.capture()
                candidate = self.controller.step(fresh)
                if (fresh_geometry != geometry or not stable_observation(observation, fresh)
                        or candidate != decision):
                    continue
                if self.args.prepare_only and decision['action'] == 'submit_event_batch':
                    self.preview_return(fresh, fresh_geometry, fresh_path)
                    break
                # [持久化] 最终胜利在返回输入前封存双帧；崩溃后只允许核对此操作，不生成新submit。
                if decision['action'] == 'return_final':
                    pending = self.ledger['pending']
                    if not pending.get('input_receipt') or not pending.get('submit_frame'):
                        raise RuntimeError('原提交实图或输入回执不完整，保留最终胜利现场。')
                    keys = ('event_id', 'mode', 'method', 'start_stage', 'quantity', 'cost', 'unit_cost',
                            'free_ap', 'free_only', 'cache_date', 'op_id', 'input_receipt')
                    pending['result_checkpoint'] = {key: copy.deepcopy(pending[key]) for key in keys}
                    pending['result_checkpoint'].update(schema_version=1,
                        submit_frame=copy.deepcopy(pending['submit_frame']),
                        victory_frames=[checkpoint_frame(path, observation), checkpoint_frame(fresh_path, fresh)])
                self.controller.commit(decision)
                if decision['action'] == 'submit_event_batch':
                    pending = self.ledger['pending']
                    pending.update(evidence=str(fresh_path), op_id=uuid.uuid4().hex,
                                   cache_date=self.config['cache_date'],
                                   submit_frame=checkpoint_frame(fresh_path, fresh))
                # Every consuming intent and settlement is durable before any next action.
                self.persist()
                if decision['action'] == 'settle_event_batch':
                    self.ledger['batches'][-1]['result_evidence'] = str(fresh_path)
                    self.ledger['batches'][-1]['free_ap_after'] = fresh.get('free_ap')
                    self.ledger['batches'][-1]['owned_extra_ap_after'] = fresh.get('owned_extra_ap')
                    self.last_change = time.monotonic()
                    self.report['actions'].append({'type': 'settle_event_batch', 'game_input': False})
                    self.persist()
                    continue
                point = decision.get('point')
                if not point or len(point) != 2:
                    raise RuntimeError('活动动作没有经识别的坐标，未发送输入。')
                receipt = self.window.click_client(
                    [point[0] * fresh_geometry['width'] / 1280,
                     point[1] * fresh_geometry['height'] / 720], fresh_geometry, press_ms=250)
                self.report['actions'].append({'type': decision['action'], 'evidence': str(fresh_path), **receipt})
                safe_entries = {'enter_event': ('main_menu', 'enter_event'),
                                'select_mode': ('event_hub', self.config['event']['mode']),
                                'open_quick_dialog': ('event_preparation', 'open_quick'),
                                'open_auto_dialog': ('event_preparation', 'open_auto')}
                self.navigation_retry = ({'action': decision['action'], 'expected': safe_entries[decision['action']],
                    'point': list(point), 'sent_at': time.monotonic(), 'used': False}
                    if decision['action'] in safe_entries else None)
                if decision['action'] == 'submit_event_batch':
                    self.ledger['pending']['input_receipt'] = receipt
                # [回执] 只有宿主实际返回输入成功后才记录；没有回执的检查点不能从准备页恢复。
                if decision['action'] == 'return_final':
                    self.ledger['pending']['result_checkpoint']['return_receipt'] = copy.deepcopy(receipt)
                self.last_change = time.monotonic()
                self.persist()
                time.sleep(.6)
            else:
                if self.retry_navigation(observation, geometry):
                    continue
                elapsed = time.monotonic() - self.last_change
                budget = network_wait_seconds(self.root)
                if self.ledger.get('pending') and observation['state'] in ('event_auto_running', 'event_victory'):
                    budget = max(budget, 300)
                if elapsed >= budget:
                    raise RuntimeError(f'活动画面已等待{budget:g}秒；保留未决记录，不重新提交。')
                self.report['waiting'] = {'elapsed_seconds': round(elapsed, 1), 'timeout_seconds': budget,
                    'message': f'等待页面或战斗结果 {elapsed:.0f}/{budget:g} 秒，不重复点击'}
                self.persist()
                time.sleep(.5)
        self.report.pop('waiting', None)
        self.persist()
        return self.report


def reconcile_visible_pending(root, output):
    """A resumed GUI command may finish an existing visible result, never submit."""
    from argparse import Namespace
    root, output = Path(root), Path(output)
    found = []
    for path in iter_cache_records(root / 'runtime'):
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        pending = value.get('pending') if isinstance(value, dict) else None
        if (isinstance(pending, dict) and pending.get('module') == 'event'
                and value.get('cache_date') == pending.get('cache_date') == game_day().isoformat()):
            found.append(path)
    if len(found) != 1:
        return None
    with exclusive_ledger(root / 'runtime/game_control'):
        output.mkdir(parents=True, exist_ok=True)
        image, _ = GameWindow.find().capture()
        probe = output / 'existing_result.png'; probe=next_path(probe);image.save(probe)
        observation = EventBattleVision().inspect_file(probe, output / 'probe_ocr')
    # [恢复入口] 已有持久化检查点的准备页交给run逐图核验；没有检查点仍保留旧pending。
    selected = json.loads(found[0].read_text(encoding='utf-8'))
    has_checkpoint = bool((selected.get('pending') or {}).get('result_checkpoint'))
    if observation['state'] not in ('event_reward', 'event_victory', 'event_auto_running', 'event_defeat') and not (
            observation['state'] in ('event_preparation','event_hub') and has_checkpoint):
        return None
    return run(Namespace(data_root=root, output=output, prepare_only=False, reconcile_pending=True))


def run(args):
    root, output = Path(args.data_root), Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    report = {'execute': True, 'completed': False, 'cache_date': game_day().isoformat(),
              'actions': [], 'observations': [], 'prepare_only': bool(args.prepare_only)}
    runner = None
    os.environ.setdefault('BD2_EXECUTION_GAME_DAY', game_day().isoformat())
    try:
        with exclusive_ledger(root / 'runtime/game_control'):
            window = GameWindow.find()
            if getattr(args, 'reconcile_pending', False):
                from date_rollover import require_current_cache
                require_current_cache(root)
                config = load_config(root / 'config/event_and_rewards.json')
                candidates = []
                for candidate in iter_cache_records(root / 'runtime'):
                    value = json.loads(candidate.read_text(encoding='utf-8'))
                    pending = value.get('pending') if isinstance(value, dict) else None
                    if (isinstance(pending, dict) and pending.get('module') == 'event'
                and value.get('cache_date') == pending.get('cache_date') == game_day().isoformat()):
                        candidates.append((candidate, value))
                if len(candidates) != 1:
                    raise RuntimeError('活动未决记录不是唯一一份，未执行任何恢复动作。')
                path, ledger = candidates[0]
                guard_foreign_pending(root / 'runtime', path, module='event')
                effective = copy.deepcopy(config)
                effective['event'].update(event_id=ledger['event_id'], mode=ledger['pending']['mode'],
                                          battle_method=ledger['pending']['method'])
                report.update(ledger=str(path), recovering_existing_batch=True,
                              recovery_scope='collect_existing_result_then_return_home_no_submit')
                runner = EventBattleRun(args, effective, ledger, path, window, report)
                return runner.execute()
            home = ensure_main_menu(window, output / 'start_home', execute=True, timeout=network_wait_seconds(root))
            if not home['completed']:
                raise RuntimeError('活动开始前尚未确认主菜单。')
            report.update(navigation_completed=True, returned_to_main_menu=True)
            from date_rollover import require_current_cache
            require_current_cache(root)
            config = load_config(root / 'config/event_and_rewards.json')
            if config['cache_date'] != game_day().isoformat():
                raise RuntimeError('已回到主菜单；活动缓存不是当前游戏日。')
            if not config['event']['enabled'] and not getattr(args,'entry_only',False):
                report.update(completed=True, skipped=True, skip_reason='活动在设置中停用，已跳过。')
                return report
            vision = EventBattleVision()
            effective = copy.deepcopy(config)
            verified=enter_and_verify_event_hub(window,vision,root,output,report,config['cache_date'],effective['event']['event_id'])
            event_id=verified['observation']['event_id'];effective['event']['event_id']=event_id
            if getattr(args,'entry_only',False):
                home=ensure_main_menu(window,output/'entry_only_return',execute=True,timeout=network_wait_seconds(root))
                report.update(completed=bool(home['completed']),returned_to_main_menu=bool(home['completed']),
                    entry_verified=True,scope='event_entry_hub_identity_and_return_no_battle_no_ledger',return_navigation=home)
                return report
            path = event_ledger_path(root, effective)
            if not args.prepare_only:
                guard_foreign_pending(root / 'runtime', path, module='event')
            ledger = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
                'schema_version': 1, 'cache_date': config['cache_date'], 'event_id': event_id,
                'pending': None, 'spent_total': 0, 'runs_by_mode': {'normal': 0, 'challenge': 0}, 'batches': []}
            if ledger.get('cache_date') != config['cache_date'] or ledger.get('event_id') != event_id:
                raise RuntimeError('活动账本日期或活动身份不一致，未覆盖旧记录。')
            if ledger.get('completed') and 'runs_by_mode' not in ledger:
                raise RuntimeError('本游戏日有旧版活动完成记录，保留原消费；请先核对，未重复开战。')
            report['ledger'] = str(path)
            report['returned_to_main_menu'] = False
            runner = EventBattleRun(args, effective, copy.deepcopy(ledger) if args.prepare_only else ledger,
                                    path, window, report)
            return runner.execute()
    except Exception as error:
        report['error'] = str(error)
        report['completed'] = False
        if runner:
            runner.persist()
        elif report.get('event_entry_receipt'):
            # [未绑定身份] 非停止故障才尝试安全返回；真实用户停止不能继续发导航输入。
            from execution_control import ExecutionControlError
            if not isinstance(error,ExecutionControlError):
                try:
                    returned=ensure_main_menu(window,output/'unbound_identity_return',execute=True,timeout=network_wait_seconds(root))
                    report['returned_to_main_menu']=bool(returned['completed']);report['return_navigation']=returned
                except Exception as return_error:report['return_error']=str(return_error)
        return report
    finally:
        save(output / 'run.json', report)
