"""Own event input, double-frame submit checks and the shared AP journal."""
from frame_store import next_path, report_write, persist_references
import copy
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
                    'total_cost', 'free_only', 'free_ap', 'owned_extra_ap')


def stable_observation(first, second):
    return all(first.get(key) == second.get(key) for key in OBSERVATION_KEYS)


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
        identity = observation.get('event_id')
        signature = (identity, point, geometry) if observation.get('state') == 'main_menu' and identity and point else None
        if signature is not None and signature == previous:
            report.pop('waiting', None)
            report['event_entry_images'] = [r['image'] for r in report['observations'][-2:]]
            persist_references(report['event_entry_images'])
            save(output / 'run.json', report)
            return observation
        previous = copy.deepcopy(signature)
        report['waiting'] = {'message': '等待主页轮播显示已识别的活动标题',
                             'timeout_seconds': network_wait_seconds(root), 'frames': index}
        save(output / 'run.json', report)
        time.sleep(.5)
    raise RuntimeError('等待主页活动轮播超时，未稳定识别当期活动标题，未进入活动。')


class EventBattleRun:
    def __init__(self, args, config, ledger, ledger_path, window, report):
        from event_battle_flow import EventBattleController
        self.args, self.root, self.config = args, Path(args.data_root), config
        self.ledger, self.path, self.window, self.report = ledger, ledger_path, window, report
        event = copy.deepcopy(config['event'])
        event.setdefault('battle_method', 'quick' if event.get('quick_battle', {}).get('enabled') else 'continuous')
        self.controller = (EventBattleController.resume_pending(event, ledger)
                           if getattr(args, 'reconcile_pending', False)
                           else EventBattleController(event, ledger))
        self.vision, self.sequence = EventBattleVision(), 0
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
                self.controller.commit(decision)
                if decision['action'] == 'submit_event_batch':
                    pending = self.ledger['pending']
                    pending.update(evidence=str(fresh_path), op_id=uuid.uuid4().hex,
                                   cache_date=self.config['cache_date'])
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
    if observation['state'] not in ('event_reward', 'event_victory', 'event_auto_running', 'event_defeat'):
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
            if not config['event']['enabled']:
                report.update(completed=True, skipped=True, skip_reason='活动在设置中停用，已跳过。')
                return report
            vision = EventBattleVision()
            effective = copy.deepcopy(config)
            event_id = effective['event']['event_id']
            if event_id in ('auto_current', 'configure_current_event'):
                observation = wait_home_event(window, vision, root, output, report, config['cache_date'])
                event_id = observation['event_id']
                effective['event']['event_id'] = event_id
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
        return report
    finally:
        save(output / 'run.json', report)
