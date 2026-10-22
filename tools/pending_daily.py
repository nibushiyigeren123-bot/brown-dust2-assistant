"""Inspect or explicitly execute the three prepared daily modules.

Execution is date-gated by configuration. Read-only inspection is the default.
"""
from frame_store import next_path, report_write, persist_references
import argparse
import hashlib
import json
import msvcrt
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from local_cache import cache_path, load_local_config

from daily_tasks import DailyController, DailyLedger, load_config, guard_foreign_pending
from daily_vision import DailyVision, inspect_frame
from m00_recognize import ROOT, read_image
from common_navigation import ensure_main_menu
from runtime_policy import network_wait_seconds


@contextmanager
def exclusive_ledger(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            from execution_control import stop_execution
            stop_execution('This local cache is already controlled by another runner', 'lock_conflict')
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def inspect(path, vision, config, output, ocr_json=None):
    payload = json.loads(ocr_json.read_text(encoding='utf-8')) if ocr_json else None
    return inspect_frame(path, vision, config, output, payload)


def same_rechecked_action(original,fresh):
    if original==fresh:
        return True
    # Nearby characters can hide different edges of the same red platform.
    # A fresh uniquely recognized platform may move its safe center slightly;
    # this exception never applies to resource-consuming submissions.
    if (original.get('type')==fresh.get('type')=='enter_mirror_arena'
            and original.get('status')==fresh.get('status')=='action'
            and not original.get('consumes') and not fresh.get('consumes')):
        return sum((a-b)**2 for a,b in zip(original['point'],fresh['point']))<=40**2
    return False


def prepare_goddess_menu(window,vision,output):
    """Establish the fixed menu starting point, including an open collection."""
    import win32api
    output.mkdir(parents=True,exist_ok=True)
    actions=[]
    for attempt in range(3):
        if win32api.GetAsyncKeyState(0x77)&0x8000:
            return {'completed':False,'stopped':'F8','collection_actions':actions}
        image,geometry=window.capture()
        path=output/f'collection_check_{attempt}.png'
        path=next_path(path);image.save(path)
        collection=vision.goddess_entry.library(read_image(path))
        if not collection: break
        fresh,fresh_geometry=window.capture()
        fresh.save(output/f'collection_preclick_{attempt}.png')
        if not vision.goddess_entry.library(read_image(output/f'collection_preclick_{attempt}.png')):
            continue
        if win32api.GetAsyncKeyState(0x77)&0x8000:
            return {'completed':False,'stopped':'F8','collection_actions':actions}
        x,y=collection['back_point']
        event=window.click_client([x*fresh_geometry['width']/1280,y*fresh_geometry['height']/720],fresh_geometry)
        actions.append({'type':'leave_collection_for_menu',**event})
        time.sleep(.9)
    result=ensure_main_menu(window,output/'menu_check',execute=True,timeout=network_wait_seconds())
    result['collection_actions']=actions
    return result


def goddess_checkpoint_complete(checkpoint, data, expected_day):
    """A deferred reward never negates a separately verified daily check-in."""
    return (checkpoint in ('check_in', 'daily_reward')
            and data.get('cache_date') == expected_day and not data.get('pending')
            and checkpoint in data.get('goddess', {}).get('done', []))


def run(args):
    config = load_config(args.config.resolve(strict=True))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    vision = DailyVision()
    if args.image:
        if args.execute:
            raise ValueError('Saved-image inspection cannot send input')
        observation = inspect(args.image.resolve(strict=True), vision, config, output, args.ocr_json)
        ledger = DailyLedger(None, config)
        result = {'execute': False, 'observation': observation,
                  'proposed': DailyController(config, ledger, args.module, args.checkpoint).step(observation)}
        report_write(output / 'inspection.json', result)
        return result
    if args.execute:
        from game_calendar import check_execution_day
        check_execution_day(config['cache_date'])
        os.environ['BD2_EXECUTION_GAME_DAY'] = config['cache_date']
        china_date = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        if china_date < config['execute_not_before']:
            raise RuntimeError('Today is preparation only; execution is disabled until the configured test date')
    ledger_path = cache_path(ROOT, config)
    from game_window import GameWindow
    import win32api
    window = GameWindow.find()
    report = {'module': args.module, 'execute': args.execute, 'cache_date':config['cache_date'], 'completed': False,
              'actions': [], 'observations': [], 'ledger': str(ledger_path) if args.execute else None}

    def save():
        report_write(output / 'run.json', report)

    with exclusive_ledger(ROOT / 'runtime/game_control'):
        if args.execute:
            guard_foreign_pending(ROOT / 'runtime', ledger_path, module=args.module)
        ledger = DailyLedger(ledger_path if args.execute else None, config)
        controller = DailyController(config, ledger, args.module, args.checkpoint)
        from runtime_policy import network_wait_seconds
        controller.network_wait_seconds = network_wait_seconds()
        entry_scene_observations=0
        deadline = time.monotonic() + max(args.timeout, controller.network_wait_seconds * 5)
        try:
            if args.module == 'hunting':
                from hunting_probe import probe_hunting_task
                def probe_frame(observation,path):
                    message='读取固定资源栏与狩猎页面，不提交战斗。' if observation.get('state') in ('hunt_map','hunt_dialog') else '开头核对每日狩猎任务进度，不领取奖励。'
                    report['observations'].append({'image':str(path),'observation':observation,'decision':{'status':'wait','reason':message}})
                    save()
            if args.execute and args.module == 'hunting' and config['hunting']['enabled'] and not ledger.data['pending']:
                progress = ledger.data['hunting']
                check=progress.get('daily_check') or {}
                audit_id=os.environ.get('BD2_DAILY_AUDIT_ID')
                reuse=bool(audit_id and check.get('audit_id')==audit_id and check.get('cache_date')==config['cache_date'] and check.get('status') in ('done','not_done'))
                if reuse or progress['daily_done'] or progress['runs'].get('daily',0):
                    report['daily_task_check']={'source':'initial_menu_audit' if reuse else 'settled_daily_cache','evidence':check}
                else:
                    evidence=probe_hunting_task(config, output/'daily_task_check', ROOT, on_frame=probe_frame)
                    progress['daily_check']=evidence
                    if evidence['status']=='done':progress['daily_done']=True
                    ledger.save()
                    report['daily_task_check']=evidence
                save()
                from hunting_survey import survey_resources
                report['resource_survey']=survey_resources(window,config,output/'resource_survey',ROOT,on_frame=probe_frame)
                save()
            wanted=config['goddess']['actions']
            if getattr(args,'entry_only',False) and ledger.data['pending']:
                raise RuntimeError('Entry-only navigation refuses an unresolved action')
            if (args.execute and args.module=='goddess' and not ledger.data['pending']
                    and config['goddess']['enabled'] and config['goddess']['user_positioned_near_statue']
                    and set(wanted)<=set(config['goddess']['actions'])
                    and not all(task in ledger.data['goddess']['done'] for task in wanted)):
                prepared=prepare_goddess_menu(window,vision,output/'prepare_menu')
                report['menu_preparation']=prepared
                save()
                if not prepared['completed']:
                    report['stopped']=prepared.get('stopped','main_menu_not_established_before_goddess')
                    return report
            if (args.execute and args.module=='mirror' and not ledger.data['pending']
                    and config['mirror']['enabled']
                    and ledger.data['mirror']['completed']<config['mirror']['max_battles']
                    and ledger.data['mirror']['spent']<config['mirror']['resource_budget']):
                startup,geometry=window.capture()
                startup_path=output/'startup.png';startup_path=next_path(startup_path);startup.save(startup_path)
                startup_obs=inspect(startup_path,vision,config,output)
                report['startup_observation']=startup_obs
                if startup_obs['state'] not in ('main_menu','goddess_collection','mirror_lobby','mirror_ready','mirror_auto_dialog'):
                    prepared=prepare_goddess_menu(window,vision,output/'prepare_menu')
                    report['menu_preparation']=prepared;save()
                    if not prepared['completed']:
                        report['stopped']='main_menu_not_established_before_mirror'
                        return report
            while time.monotonic() < deadline:
                if win32api.GetAsyncKeyState(0x77) & 0x8000:
                    report['stopped'] = 'F8'
                    break
                image, geometry = window.capture()
                path = output / f"frame_{len(report['observations']):03}.png"
                path=next_path(path);image.save(path)
                observation = inspect(path, vision, config, output)
                from frame_store import active
                if active():
                    observation['image_evidence'] = str(path)
                if args.module == 'hunting':
                    from hunting_resources import retain_observation
                    retain_observation(ROOT, observation, path, config['cache_date'])
                if (args.execute and getattr(args,'entry_only',False)
                        and observation['state'] in ('goddess_scene','goddess_options')
                        and controller.goddess_scene_attempts):
                    entry_scene_observations+=1
                    report['observations'].append({'image':str(path),'geometry':geometry,
                        'observation':observation,'decision':{'status':'entry_verified' if entry_scene_observations>=2 else 'wait_for_stable_scene'}})
                    if entry_scene_observations<2:
                        save()
                        time.sleep(.25)
                        continue
                    report['entry_completed']=True
                    report['verification_scope']='entry_only_no_interaction'
                    returned=ensure_main_menu(window,output/'return_to_menu',execute=True,timeout=network_wait_seconds())
                    report['returned_to_main_menu']=returned['completed']
                    report['completed']=returned['completed']
                    if not returned['completed']: report['stopped']='entry_verified_but_return_to_menu_not_verified'
                    break
                if getattr(args,'entry_only',False):
                    entry_scene_observations=0
                    if observation['state'] in ('goddess_options','goddess_scene'):
                        report['stopped']='entry_only_scene_not_reached_by_fixed_route'
                        break
                if args.execute and args.module == 'mirror' and observation['state'] in ('mirror_auto_dialog', 'mirror_multiplier_dialog', 'mirror_battle', 'mirror_result', 'mirror_result_footer'):
                    if ledger.data['mirror'].pop('availability', None) is not None:
                        ledger.save()
                decision = controller.step(observation)
                report['observations'].append({'image': str(path), 'geometry': geometry,
                                               'observation': observation, 'decision': decision})
                save()
                if not args.execute:
                    break
                if decision['status'] == 'action':
                    if getattr(args,'entry_only',False) and (decision.get('consumes') or decision.get('type') not in ('open_goddess_collection','enter_goddess_square')):
                        report['stopped']='entry_only_refused_non_navigation_action'
                        break
                    # OCR takes time. Re-read the current UI immediately before
                    # committing an input, so an old cost or moving panel cannot
                    # authorize a later click merely because geometry is stable.
                    fresh, fresh_geometry = window.capture()
                    fresh_path = output / f"preclick_{len(report['observations']):03}.png"
                    fresh_path=next_path(fresh_path);fresh.save(fresh_path)
                    fresh_observation = inspect(fresh_path, vision, config, output)
                    if args.module == 'hunting':
                        retain_observation(ROOT, fresh_observation, fresh_path, config['cache_date'])
                    fresh_decision = controller.step(fresh_observation)
                    report['observations'][-1]['preclick'] = {'image': str(fresh_path),
                        'observation': fresh_observation, 'decision': fresh_decision}
                    if not same_rechecked_action(decision,fresh_decision):
                        report['observations'][-1]['input_skipped'] = 'UI or proposed action changed during verification'
                        save()
                        continue
                    decision=fresh_decision
                    geometry = fresh_geometry
                    if win32api.GetAsyncKeyState(0x77) & 0x8000:
                        report['stopped'] = 'F8'
                        break
                    if decision.get('consumes'):
                        if active():
                            decision['image_evidence'] = {'before':str(path), 'preclick':str(fresh_path)}
                        ledger.submit_intent(decision)
                    x, y = decision['point']
                    receipt = window.click_client([x * geometry['width'] / 1280, y * geometry['height'] / 720], geometry)
                    report['actions'].append({**decision, **receipt})
                    controller.after_action(decision)
                    if decision.get('consumes'):
                        ledger.data['pending']['input_receipt'] = receipt
                        ledger.save()
                    save()
                    time.sleep(0.9)
                elif decision['status'] == 'finished':
                    if decision.get('skipped'):
                        report.update(skipped=True,unavailable=decision.get('unavailable'),skip_reason=decision.get('reason'))
                    deferred=decision.get('deferred_actions',[])
                    report['module_completed'] = not deferred
                    if args.module == 'goddess' and args.checkpoint in ('check_in','daily_reward'):
                        report['completion_checkpoint'] = args.checkpoint
                        report['module_completed'] = goddess_checkpoint_complete(args.checkpoint, ledger.data, config['cache_date'])
                    report['deferred_actions'] = deferred
                    report['completion_scope'] = decision.get('completion_scope','requested_tasks')
                    report['completion_reason'] = decision.get('reason')
                    if args.module == 'goddess' and args.checkpoint in deferred:
                        report['completion_reason'] = ('尚未识别并领取女神像今日奖励，已返回主菜单；该项保留未完成，请核对叹号入口画面。'
                            if args.checkpoint == 'daily_reward' else '当前没有可用的女神像签到互动，签到尚未核实。')
                    returned = ensure_main_menu(window, output/'return_to_menu', execute=True, timeout=network_wait_seconds())
                    report['returned_to_main_menu'] = returned['completed']
                    report['completed'] = returned['completed']
                    if args.module == 'mirror':
                        from mirror_status import settlement_from_report
                        report['progress'] = ledger.data
                        availability = settlement_from_report(report, config['cache_date'])
                        if availability and not ledger.data.get('pending'):
                            availability['source_run'] = str(output)
                            ledger.data['mirror']['availability'] = availability
                            ledger.save()
                    if not returned['completed']:
                        report['stopped'] = 'task_verified_but_return_to_menu_not_verified'
                    break
                elif decision['status'] not in ('wait',):
                    if decision['status']=='skipped':
                        report['skipped']=True
                    report['stopped'] = decision
                    break
                time.sleep(0.25)
            if args.execute and not report['completed'] and 'stopped' not in report:
                report['stopped'] = 'timeout_without_verified_completion'
        except Exception as error:
            report['error'] = str(error)
            raise
        finally:
            report['progress'] = ledger.data
            save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module', required=True, choices=['hunting', 'goddess', 'mirror'])
    parser.add_argument('--checkpoint', default='available', choices=['available', 'check_in', 'daily_reward'])
    parser.add_argument('--config', type=Path, default=ROOT / 'config/daily_tasks.json')
    parser.add_argument('--image', type=Path)
    parser.add_argument('--ocr-json', type=Path, help='Use a retained OCR result for offline review')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--entry-only',action='store_true',help='Verify square entry and return only; never interact')
    parser.add_argument('--timeout', type=float, default=300)
    args = parser.parse_args()
    if args.entry_only and args.module!='goddess': parser.error('--entry-only is limited to goddess navigation')
    result = run(args)
    print(json.dumps({k: result.get(k) for k in ('execute', 'completed', 'observation', 'proposed', 'stopped')}, ensure_ascii=False))
    if args.execute and not result.get('completed'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
