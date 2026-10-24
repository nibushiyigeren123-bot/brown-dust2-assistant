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


def inspect_goddess_window(path,vision,config,output,controller,module):
    """[期限接线] 广场回执后和恢复女神像未决时走纯像素；未知/遮挡不触发慢OCR后备。"""
    pending=controller.ledger.data.get('pending') or {}
    if module=='goddess' and (controller.goddess_scene_attempts or pending.get('module')=='goddess'):
        if controller.goddess_window_expired():return {'state':'unknown','reason':'goddess_absolute_window_expired'}
        return vision.inspect_goddess_fast(read_image(path),square_entered=True)
    return inspect(path,vision,config,output)


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


def capture_statue_result_frames(window,geometry,output,expected_day):
    """[结果保全] 回执落盘后先采有界即时帧再OCR；停止、换日、窗口变化立即中止，不发输入。"""
    from game_window import check_requested_stop
    from game_calendar import check_execution_day
    frames=[];started=time.monotonic()
    for index,offset in enumerate((0,.2,.5,.9,1.5)):
        delay=offset-(time.monotonic()-started)
        if delay>0:time.sleep(delay)
        check_requested_stop();check_execution_day(expected_day)
        image,current=window.capture()
        if current!=geometry:raise RuntimeError('女神像结果采集窗口几何改变，保留原pending。')
        path=next_path(Path(output)/f'statue_result_{index:02}.png');image.save(path)
        persist_references(str(path))
        frames.append({'image':str(path),'geometry':current,'seconds_after_receipt':round(time.monotonic()-started,3)})
    return frames


def return_home_with_unresolved_reward(window,output,ledger,decision,report):
    """[受限恢复] 仅原女神像零费用签到/领奖未决允许返主页；不保存或核销账本，不报告业务完成。"""
    pending=ledger.data.get('pending') or {}
    if (decision.get('status')!='needs_review' or decision.get('return_home_preserving_pending') is not True
            or pending.get('module')!='goddess' or pending.get('task') not in ('check_in','daily_reward')
            or pending.get('cost')!=0 or pending.get('quantity')!=1 or not pending.get('input_receipt')
            or pending.get('config_hash')!=ledger.config_hash):
        raise RuntimeError('女神像未决返回决策身份不符，不执行导航。')
    before=json.dumps(pending,sort_keys=True,ensure_ascii=False)
    returned=ensure_main_menu(window,Path(output)/'return_with_pending',execute=True,timeout=network_wait_seconds())
    if json.dumps(ledger.data.get('pending'),sort_keys=True,ensure_ascii=False)!=before:
        raise RuntimeError('返回导航意外改变原pending，停止。')
    report.update(completed=False,module_completed=False,returned_to_main_menu=bool(returned.get('completed')),
                  pending_preserved=True,stopped=decision,return_navigation=returned)
    if report.get('goddess_wait_windows'):
        trace=report['goddess_wait_windows'][-1];trace['home_verification_finished_monotonic']=time.monotonic()
        trace['return_navigation_seconds']=round(time.monotonic()-trace.get('return_requested_monotonic',time.monotonic()),3)


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


def reconcile_visible_hunting_pending(root, output):
    """On explicit hunting execution, reconcile its result before home navigation.

    No submission or cleanup is sent here. Unknown evidence leaves pending intact.
    This is never called for read-only inspection or another queue command.
    """
    root, output = Path(root), Path(output)
    config = load_config(root / 'config/daily_tasks.json')
    path = cache_path(root, config)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding='utf-8'))
    pending = data.get('pending')
    if not pending or pending.get('module') != 'hunting':
        return None
    from game_calendar import check_execution_day
    from game_window import GameWindow, check_requested_stop
    check_execution_day(config['cache_date'])
    output.mkdir(parents=True, exist_ok=True)
    report = {'execute':True, 'completed':False, 'reconciled':False,
        'scope':'existing_hunting_receipt_only', 'actions':[], 'observations':[]}
    def save_report():
        report_write(output / 'run.json', report)
    with exclusive_ledger(root / 'runtime/game_control'):
        guard_foreign_pending(root / 'runtime', path, module='hunting')
        ledger = DailyLedger(path, config)
        pending = ledger.data['pending']
        if (pending.get('type') != 'submit_quick_hunt' or pending.get('consumes') is not True
                or (pending.get('input_receipt') or {}).get('input_kind') != 'left_click'):
            report['stopped'] = '狩猎提交回执不完整；保留pending，未点击。'
            save_report()
            return report
        window, vision = GameWindow.find(), DailyVision()
        controller = DailyController(config, ledger, 'hunting')
        previous = None
        for index in range(4):
            check_requested_stop()
            check_execution_day(config['cache_date'])
            image, geometry = window.capture()
            frame = next_path(output / f'result_{index}.png')
            image.save(frame)
            obs = inspect(frame, vision, config, output)
            obs['image_evidence'] = str(frame)
            report['observations'].append({'image':str(frame), 'window':geometry, 'observation':obs})
            if obs['state'] != 'hunt_reward':
                report['stopped'] = '未看到属于当前提交的狩猎结果；保留pending，未点击。'
                break
            signature = (geometry, tuple(obs.get(k) for k in
                ('completed_runs','free_remaining','owned_extra','extra_approximate')))
            if signature == previous:
                decision = controller.step(obs)
                report['decision'] = decision
                if ledger.data['pending'] is None:
                    report.update(reconciled=True, completed=True, pending=None)
                    break
                if decision['status'] == 'needs_review':
                    report['stopped'] = decision['reason']
                    break
            previous = signature
            save_report()
            time.sleep(.25)
        if not report['reconciled'] and 'stopped' not in report:
            report['stopped'] = '狩猎结果复读后仍未核实；保留pending，未点击。'
        report['pending'] = ledger.data['pending']
        save_report()
    return report


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
    # [来源] 保留实际加载的镜中策略，供下游独立核对资源结束证据。
    if args.module == 'mirror': report['mirror_policy'] = dict(config['mirror'])

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
                # 总览已分别确认米饭与火把两帧，传给本次控制器决定免费耗尽跳过。
                controller.resource_survey=report['resource_survey']['resources']
                save()
            wanted=config['goddess']['actions']
            if getattr(args,'entry_only',False) and ledger.data['pending']:
                raise RuntimeError('Entry-only navigation refuses an unresolved action')
            if (args.execute and args.module=='goddess' and not ledger.data['pending']
                    and config['goddess']['enabled']
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
                    # [全队列停止] 使用已命中的这次F8结果落盘控制信号，松键后父队列仍能停止。
                    from execution_control import stop_execution
                    stop_execution('用户已按F8停止当前任务', 'stopped')
                # [墙钟上界] 到期先派返回决策，不再截图或进入OCR；截图后也再次检查期限。
                expired=controller.goddess_window_expired() if args.module=='goddess' else None
                if expired:
                    path=None;geometry=None
                    observation={'state':'unknown','reason':'goddess_absolute_window_expired'}
                else:
                    image, geometry = window.capture()
                    path = output / f"frame_{len(report['observations']):03}.png"
                    path=next_path(path);image.save(path)
                    observation = inspect_goddess_window(path,vision,config,output,controller,args.module)
                    if args.module=='goddess':report['goddess_last_visual_state']=observation['state']
                from frame_store import active
                if active() and path is not None:
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
                if args.module=='goddess' and controller.goddess_button_deadline is not None:
                    # [实测计时] 等按钮到期与返主页双帧耗时分开记录，禁止将两者混成五秒。
                    windows=report.setdefault('goddess_wait_windows',[])
                    if not windows or windows[-1]['deadline_monotonic']!=controller.goddess_button_deadline:
                        windows.append({'started_monotonic':controller.goddess_button_deadline-5,
                            'deadline_monotonic':controller.goddess_button_deadline,'origin':'restored_pending_first_observation'})
                    if decision.get('return_home_preserving_pending') or decision.get('outcome')=='goddess_button_window_expired':
                        windows[-1].update(return_requested_monotonic=time.monotonic(),
                            wait_elapsed_seconds=round(time.monotonic()-windows[-1]['started_monotonic'],3),
                            reason=decision['reason'],last_visual_state=report.get('goddess_last_visual_state',observation['state']))
                report['observations'].append({'image': str(path) if path is not None else None, 'geometry': geometry,
                                               'observation': observation, 'decision': decision})
                save()
                if not args.execute:
                    break
                # 当前两帧资源归零后，只做分类导航总览；pending由controller.step优先处理，不能借归零清账。
                if decision.get('requires_resource_survey') and args.module=='hunting':
                    from hunting_survey import survey_resources
                    report['resource_survey']=survey_resources(window,config,output/'resource_recheck',ROOT,on_frame=probe_frame)
                    controller.resource_survey=report['resource_survey']['resources']
                    save()
                    continue
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
                    fresh_observation = inspect_goddess_window(fresh_path,vision,config,output,controller,args.module)
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
                    if args.module=='goddess' and controller.goddess_window_expired():
                        report['observations'][-1]['input_skipped']='goddess_absolute_window_expired_before_input'
                        save()
                        continue
                    if decision.get('consumes'):
                        if active():
                            decision['image_evidence'] = {'before':str(path), 'preclick':str(fresh_path)}
                        ledger.submit_intent(decision)
                    x, y = decision['point']
                    click_options = {'native_motion': True, 'press_ms': 250} if decision['type'] == 'dismiss_hunt_reward' else {}
                    receipt = window.click_client([x * geometry['width'] / 1280, y * geometry['height'] / 720], geometry, **click_options)
                    report['actions'].append({**decision, **receipt})
                    controller.after_action({**decision,'input_receipt':receipt})
                    if decision['type'] in ('enter_goddess_square','interact_statue'):
                        report.setdefault('goddess_wait_windows',[]).append({
                            'started_monotonic':controller.goddess_button_deadline-5,
                            'deadline_monotonic':controller.goddess_button_deadline,
                            'origin':decision['type']+'_receipt','input_receipt':receipt})
                    if decision.get('consumes'):
                        ledger.data['pending']['input_receipt'] = receipt
                        ledger.save()
                    save()
                    if decision['type']=='interact_statue':
                        frames=capture_statue_result_frames(window,geometry,output,config['cache_date'])
                        report.setdefault('statue_result_frames',[]).extend(frames);save()
                        # [结果核对] 采集完成才OCR；只让现有结果合同结算同一pending，不派发下一步动作。
                        for frame in frames:
                            if not ledger.data.get('pending'):break
                            from game_window import check_requested_stop
                            check_requested_stop()
                            result_observation=inspect_goddess_window(Path(frame['image']),vision,config,output,controller,args.module)
                            result_decision=controller.step(result_observation)
                            report['observations'].append({**frame,'observation':result_observation,
                                'decision':result_decision,'scope':'post_statue_input_result_only'})
                            save()
                    else:time.sleep(0.9)
                elif decision['status'] == 'finished':
                    if decision.get('skipped'):
                        report.update(skipped=True,unavailable=decision.get('unavailable'),skip_reason=decision.get('reason'))
                    deferred=decision.get('deferred_actions',[])
                    report['module_completed'] = decision.get('module_completed', not deferred)
                    # [输出] 结束证据交给队列与进度共同复核，不改实际战斗完成账本。
                    if decision.get('mirror_resource_end'): report['mirror_resource_end'] = decision['mirror_resource_end']
                    if args.module == 'goddess' and args.checkpoint in ('check_in','daily_reward'):
                        report['completion_checkpoint'] = args.checkpoint
                        report['module_completed'] = goddess_checkpoint_complete(args.checkpoint, ledger.data, config['cache_date'])
                    report['deferred_actions'] = deferred
                    report['completion_scope'] = decision.get('completion_scope','requested_tasks')
                    if decision.get('free_exhaustion'):report['free_exhaustion']=decision['free_exhaustion']
                    # 正常返回与到账核实分开；已结束的零费尝试完整留在events，不写领取完成。
                    if decision.get('outcome'):report['outcome']=decision['outcome']
                    report['completion_reason'] = decision.get('reason')
                    if args.module == 'goddess' and args.checkpoint in deferred:
                        report['completion_reason'] = ('尚未识别并领取女神像今日奖励，已返回主菜单；该项保留未完成，请核对叹号入口画面。'
                            if args.checkpoint == 'daily_reward' else '当前没有可用的女神像签到互动，签到尚未核实。')
                    returned = ensure_main_menu(window, output/'return_to_menu', execute=True, timeout=network_wait_seconds())
                    if report.get('goddess_wait_windows'):
                        trace=report['goddess_wait_windows'][-1]
                        trace['home_verification_finished_monotonic']=time.monotonic()
                        trace['return_navigation_seconds']=round(time.monotonic()-trace.get('return_requested_monotonic',time.monotonic()),3)
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
                elif decision['status'] == 'skipped':
                    # [设置停用] 只跳过没有未决的本段；返回主页完成不等于写入任务完成。
                    if ledger.data.get('pending'):
                        report['stopped'] = {'status': 'needs_review',
                            'reason': '停用模块仍存在未决操作，保留回执待核对。'}
                        break
                    report.update(skipped=True, skip_reason=decision.get('reason'),
                                  completion_scope='disabled_in_settings')
                    returned = ensure_main_menu(window, output/'return_to_menu',
                                                execute=True, timeout=network_wait_seconds())
                    report['returned_to_main_menu'] = returned['completed']
                    report['completed'] = returned['completed']
                    if not returned['completed']:
                        report['stopped'] = 'disabled_but_return_to_menu_not_verified'
                    break
                elif decision.get('return_home_preserving_pending') is True:
                    return_home_with_unresolved_reward(window,output,ledger,decision,report)
                    save()
                    break
                elif decision['status'] not in ('wait',):
                    report['stopped'] = decision
                    break
                # [精确等待] 最后一次轮询睡眠不跨过女神像绝对截止，避免额外固定等待。
                pause=.25
                if args.module=='goddess' and controller.goddess_button_deadline is not None:
                    pause=max(0,min(pause,controller.goddess_button_deadline-time.monotonic()))
                time.sleep(pause)
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
