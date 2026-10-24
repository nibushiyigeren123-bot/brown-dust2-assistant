"""Portable GUI and isolated worker entry points for the prepared modules."""
import argparse
import importlib
import json
import os
os.environ["OPENBLAS_NUM_THREADS"] = "1"
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from project_paths import ROOT
from task_feedback import worker_feedback


from module_catalog import task_rows, BY_ID
TASKS = task_rows()


def resolve_worker_task(task_id):
    """[兼容] 可见任务只保留11项；旧workflow的日奖ID仍精确分发原worker参数。"""
    task=next((row for row in TASKS if row[0]==task_id),None)
    if task is None and task_id in BY_ID:
        module=BY_ID[task_id]
        task=(module.id,module.name,'历史兼容',module.description,module.worker,list(module.arguments))
    return task


def worker(task_id, arguments):
    task = resolve_worker_task(task_id)
    if task is None or task[4] is None:
        raise ValueError('该功能尚无可执行模块')
    output = Path(arguments[arguments.index('--output')+1]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    from frame_store import activate, finish
    activate(output)
    os.environ['BD2_QUEUE_TASK'] = task_id
    reuse_inspection = '--execute' not in arguments and output.is_relative_to((ROOT / 'evidence/inspections').resolve())
    if reuse_inspection:
        for name in ('run.json', 'inspection.json', 'worker_failure.json', 'worker_result.json',
                     'control.json', 'item_problem.json'):
            (output / name).unlink(missing_ok=True)
    # Windowed executables have no stdout by default. Keep a real UTF-8 log.
    log = (output/'worker.log').open('w' if reuse_inspection else 'a', encoding='utf-8', buffering=1)
    sys.stdout = sys.stderr = log
    sys.argv = [sys.argv[0], *task[5], *arguments]
    # [单图导航] 外层保留worker目录，内层--image诊断写inspection.json；只读不触碰游戏。
    if task_id == 'navigation' and '--image' in arguments and '--execute' not in arguments:
        position=sys.argv.index('--output')+1
        sys.argv[position]=str(output/'inspection.json')
    code = 0
    worker_day = None
    os.environ.setdefault('BD2_CONTROL_FILE', str(output / 'control.json'))
    os.environ.setdefault('BD2_ITEM_PROBLEM_FILE', str(output / 'item_problem.json'))
    try:
        # [功能] 仅安装本worker的步骤识图包装；候选只路由局部复核，原动作/账本门禁保持。
        # 禁用、缺缓存或模型异常由包装层回到原流程；此处不启动模型服务或游戏任务。
        try:
            from step_vision_hooks import install as install_step_vision
            install_step_vision(ROOT, task_id)
        except Exception:
            # 可选插件未完整打包也回原流程；不得把此情形声称步骤模型已启用。
            traceback.print_exc()
        from local_cache import load_local_config
        from game_calendar import game_day
        navigation_first = task_id in ('navigation', 'workflow')
        worker_day = game_day().isoformat() if navigation_first else load_local_config(ROOT / 'config/daily_tasks.json')['cache_date']
        if task_id != 'workflow' and '--image' not in arguments:
            from window_access import preflight
            from pending_daily import exclusive_ledger
            with exclusive_ledger(ROOT / 'runtime/game_control'):
                preflight(focus='--execute' in arguments)
        if '--execute' in arguments:
            # [功能] 启动前确认游戏日；普通模块先回主页，合法公会未决交本人先读当前页。
            os.environ['BD2_EXECUTION_GAME_DAY'] = game_day().isoformat()
            guild_recovery = False
            if task_id == 'guild':
                # [分支] 合法公会pending先交本人读当前页，避免通用导航关闭待核实收据。
                # 未知协议在任何导航前阻断；本人run还会复核，不能只信CLI标志。
                from local_cache import cache_path
                from daily_tasks import guild_pending_phase, PendingConflictError
                from pending_daily import exclusive_ledger
                with exclusive_ledger(ROOT / 'runtime/game_control'):
                    ledger = cache_path(ROOT, {'cache_date': worker_day}, 'guild')
                    data = json.loads(ledger.read_text(encoding='utf-8')) if ledger.exists() else {}
                    # [校验] 未知结构/阶段不能交给通用导航；锁内读取不改原账本。
                    if not isinstance(data, dict):
                        raise PendingConflictError('Guild ledger is not an object; retain it for review')
                    if data.get('pending') is not None:
                        if guild_pending_phase(data, game_day().isoformat(), path=ledger, directory=ROOT/'runtime') is None:
                            raise PendingConflictError('Guild pending identity invalid; recovery blocked before navigation')
                        guild_recovery = True
                        # [调用] 仅给公会模块传显式恢复开关，模块内部仍须再次校验。
                        sys.argv.append('--recover-pending')
            if task_id == 'event':
                from event_battle_runtime import reconcile_visible_pending
                recovered = reconcile_visible_pending(ROOT, output / 'pending_recovery')
                if recovered is not None:
                    # Continue the already submitted command's receipt before
                    # any new task. The recovery can only settle and return home.
                    (output / 'run.json').write_text(json.dumps(recovered, ensure_ascii=False, indent=2), encoding='utf-8')
                    code = 0 if recovered.get('completed') else 1
                    return code
            if task_id == 'hunting':
                from pending_daily import reconcile_visible_hunting_pending
                reconciled = reconcile_visible_hunting_pending(ROOT, output / 'pending_recovery')
                if reconciled is not None and not reconciled.get('reconciled'):
                    (output / 'run.json').write_text(json.dumps(dict(reconciled, blocked=True), ensure_ascii=False, indent=2), encoding='utf-8')
                    raise RuntimeError(reconciled.get('stopped') or '狩猎未决结果尚未核实。')
            if not navigation_first and not guild_recovery:
                # [分支] 无公会pending沿用原启动归主页流程；恢复时直接读当前画面。
                from common_navigation import ensure_main_menu
                from game_window import GameWindow
                from pending_daily import exclusive_ledger
                from runtime_policy import network_wait_seconds
                with exclusive_ledger(ROOT / 'runtime/game_control'):
                    home = ensure_main_menu(GameWindow.find(), output / 'launch_home', execute=True,
                                            timeout=network_wait_seconds(ROOT))
                if not home['completed']:
                    (output / 'run.json').write_text(json.dumps({
                        'execute': True, 'completed': False, 'actions': [], 'blocked': True,
                        'stopped': '当前模块启动前尚未连续确认主菜单。',
                        'navigation': home}, ensure_ascii=False, indent=2), encoding='utf-8')
                    raise RuntimeError('当前模块启动前尚未连续确认主菜单。')
            from date_rollover import require_current_cache
            os.environ['BD2_EXECUTION_GAME_DAY'] = worker_day if navigation_first else require_current_cache(ROOT)
        module = importlib.import_module(task[4])
        if task[0] in ('navigation','management','free_draw'):
            from daily_tasks import guard_foreign_pending
            from pending_daily import exclusive_ledger
            with exclusive_ledger(ROOT/'runtime/game_control'):
                if '--execute' in arguments and task_id != 'navigation':
                    guard_foreign_pending(ROOT/'runtime',ROOT/'runtime/launcher_nonledger.json', module=task_id)
                module.main()
                if task_id == 'navigation' and '--execute' in arguments and '--image' not in arguments:
                    from navigation_startup import finish_at_main_menu
                    finish_at_main_menu(ROOT, output)
        else:
            module.main()
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else 1
    except Exception as error:
        traceback.print_exc()
        code = 1
        from atomic_json import write_json
        path = output / 'worker_failure.json'
        report = {}
        report.update(execute='--execute' in arguments, completed=False,
                      cache_date=worker_day, error=str(error))
        if getattr(error, 'kind', None):
            report['control_kind'] = error.kind
        from daily_tasks import PendingConflictError
        if isinstance(error, PendingConflictError):
            report['blocked'] = True
        write_json(path, report)
    finally:
        # [状态] 任务结束释放本批CPU会话和包装，避免复用其他模块的步骤上下文。
        try:
            from step_vision_hooks import uninstall as uninstall_step_vision
            uninstall_step_vision(output)
        except Exception:
            traceback.print_exc()
        finish(failed=code != 0)
        (output/'worker_result.json').write_text(json.dumps({'task':task_id, 'exit_code':code, 'cache_date':worker_day,
            'execute':'--execute' in arguments},ensure_ascii=False,indent=2),encoding='utf-8')
        log.flush()
    return code


def self_check(output):
    import copy
    from common_navigation import PageDiagnosis
    from m01_management import ManagementRecognizer
    from m02_free_draw import FreeDrawRecognizer
    from daily_vision import DailyVision, inspect_frame
    from phase08_vision import Phase08Vision
    from m00_recognize import read_image
    from daily_tasks import load_config
    from event_reward_rules import load_config as load_event
    for cls in (PageDiagnosis, ManagementRecognizer, FreeDrawRecognizer, DailyVision, Phase08Vision):
        cls()
    import numpy as np
    # Execute the calibration-dependent branch on a synthetic image, without game input.
    PageDiagnosis().find_home(np.zeros((720, 1280, 3), dtype=np.uint8))
    hunt_config=copy.deepcopy(load_config(ROOT/'config/daily_tasks.json'))
    load_event(ROOT/'config/event_and_rewards.json')
    assert (ROOT/'tools/windows_ocr.ps1').is_file()
    if not (ROOT/'assets/launcher/sample_menu.jpg').is_file():
        # Public distributions omit personal full-screen acceptance fixtures.
        # This checks loaders/config/rules only; saved-image acceptance is separate.
        from equipment_vision import EquipmentVision
        from task_reward_vision import TaskRewardVision
        from pass_reward_vision import PassRewardVision
        from event_battle_vision import EventBattleVision
        from event_battle_flow import EventBattleController
        from module_catalog import MODULES, default_plan, validate_plan
        from equipment_rules import decompose_candidate_allowed
        for cls in (EquipmentVision, TaskRewardVision, PassRewardVision, EventBattleVision):
            cls()
        assert callable(EventBattleController.step) and callable(EventBattleController.commit)
        # [自检] 默认队列须逐项匹配当前可执行catalog，合并女神后不再依赖旧固定12项。
        registry_ids=[module.id for module in MODULES if module.worker]
        assert validate_plan(default_plan())==registry_ids
        card = {'quality':'R', 'locked':False, 'equipped':False}
        assert decompose_candidate_allowed(card, ['R']) and not decompose_candidate_allowed(card, ['N', 'R'])
        report = {'frozen': bool(getattr(sys, 'frozen', False)), 'template_loaders': 9,
                  'workbench_registry_loaded': True, 'workbench_module_count':len(registry_ids), 'config_validated': True, 'navigation_calibration_probe': 'synthetic_black_frame',
                  'equipment_rules_single_quality': True, 'personal_fixtures_shipped': False,
                  'event_battle_templates_and_controller': True,
                  'saved_image_acceptance_run': False, 'game_input': False}
        (output/'self_check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        return
    assert PageDiagnosis().inspect(read_image(ROOT/'assets/launcher/sample_menu.jpg'))['state']=='main_menu'
    receipt=Phase08Vision().inspect_file(ROOT/'assets/launcher/sample_guild_receipt.jpg',output/'ocr_check')
    assert receipt['state']=='guild_check_in_receipt', receipt
    animation=FreeDrawRecognizer().inspect(read_image(ROOT/'assets/launcher/sample_free_draw_animation.png'))
    assert animation['state']=='draw_animation' and animation.get('skip_point'), animation
    live_receipt=Phase08Vision().inspect_file(ROOT/'assets/launcher/sample_guild_live_receipt.png',output/'guild_live_check')
    assert live_receipt['state']=='guild_check_in_receipt', live_receipt
    hunt_config['hunting']['extra_plans']=[{'id':'self_check_min','category':'圣石洞穴','stage':None,
        'difficulty':'极难','runs':1,'free_only':True,'target_mode':'lowest_stock'}]
    hunt_vision=DailyVision()
    stones=inspect_frame(ROOT/'assets/launcher/sample_stones_map.jpg',hunt_vision,hunt_config,output/'hunting')
    assert stones.get('stone_inventory')==dict(zip(('火之洞穴','水之洞穴','风之洞穴','光之洞穴','暗之洞穴'),(7565,5954,5729,7959,6265))),stones
    hunt_price=inspect_frame(ROOT/'assets/launcher/sample_hunt_price.jpg',hunt_vision,hunt_config,output/'hunting')
    assert hunt_price.get('count')==1 and hunt_price.get('cost')==6 and hunt_price.get('free_only') is True,hunt_price
    middle=inspect_frame(ROOT/'assets/launcher/sample_hunt_middle.jpg',hunt_vision,hunt_config,output/'hunting')
    assert middle.get('difficulty')=='中游',middle
    mirror_editor=inspect_frame(ROOT/'assets/launcher/sample_mirror_editor_one.png',hunt_vision,hunt_config,output/'mirror')
    assert mirror_editor['state']=='mirror_multiplier_dialog' and mirror_editor['multiplier']==1,mirror_editor
    mirror_auto=inspect_frame(ROOT/'assets/launcher/sample_mirror_auto_one.png',hunt_vision,hunt_config,output/'mirror')
    assert mirror_auto['state']=='mirror_auto_dialog' and mirror_auto['multiplier']==1 and mirror_auto['cost']==1,mirror_auto
    mirror_ready=inspect_frame(ROOT/'assets/launcher/sample_mirror_ready.png',hunt_vision,hunt_config,output/'mirror')
    assert mirror_ready['state']=='mirror_ready',mirror_ready
    from equipment_vision import EquipmentVision
    from equipment_rules import decompose_candidate_allowed
    equipment_vision=EquipmentVision()
    equipment_filter=equipment_vision.inspect_file(ROOT/'assets/launcher/sample_equipment_filter_r.png',output/'equipment')
    assert equipment_filter['state']=='equipment_filter' and equipment_filter['selected_qualities']==['R']
    equipment_result=equipment_vision.inspect_file(ROOT/'assets/launcher/sample_equipment_result.png',output/'equipment')
    assert equipment_result['state']=='equipment_decompose_result' and equipment_result['matches_reviewed_55_5_result']
    equipment_default=equipment_vision.inspect_file(ROOT/'assets/launcher/sample_equipment_default.png',output/'equipment')
    assert equipment_default['state']=='equipment_grid' and equipment_default['inventory']['value']==904
    equipment_card={'quality':'R','locked':False,'equipped':False}
    assert decompose_candidate_allowed(equipment_card,['R']) and not decompose_candidate_allowed(equipment_card,['N','R'])
    from task_reward_vision import TaskRewardVision
    TaskRewardVision()
    from pass_reward_vision import PassRewardVision
    from module_catalog import MODULES, default_plan, validate_plan
    PassRewardVision()
    # [自检] 私有样本分支沿用同一有效catalog合同，不因样本是否随包改变队列数量。
    registry_ids=[module.id for module in MODULES if module.worker]
    assert validate_plan(default_plan())==registry_ids
    report={'workbench_registry_loaded':True,'workbench_module_count':len(registry_ids),'pass_reward_recognizer_loaded':True,'task_reward_recognizer_loaded':True,'frozen':bool(getattr(sys,'frozen',False)),'data_root':str(ROOT),
            'template_loaders':5,'menu_sample':'main_menu','guild_sample':receipt['state'],
            'native_windows_ocr':True,'free_draw_skip_recognized':True,'guild_live_receipt_recognized':True,
            'hunting_stones_inventory_recognized':True,'hunting_single_price_recognized':True,
            'hunting_middle_difficulty_recognized':True,'mirror_one_multiplier_and_price_recognized':True,
            'mirror_ready_recognized':True,'equipment_saved_R_recognized':True,'equipment_result_recognized':True,
            'equipment_default_inventory':904,'equipment_rules_single_quality':True,'game_input':False}
    (output/'self_check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


def main():
    # [入口] 冻结与源码共用真实缓存检查，分流在迁移/GUI/窗口发现之前。
    if '--step-vision-self-check' in sys.argv:
        parser=argparse.ArgumentParser()
        parser.add_argument('--step-vision-self-check',type=Path,required=True)
        parser.add_argument('--step-vision-image',type=Path)
        from step_vision import self_check as step_self_check
        args=parser.parse_args()
        step_self_check(ROOT,args.step_vision_self_check,args.step_vision_image)
        return 0
    # [入口] 离线全图检查先于配置迁移；不查游戏窗口、不运行worker、不触碰真实账本。
    if '--step-vision-check' in sys.argv:
        parser=argparse.ArgumentParser()
        parser.add_argument('--step-vision-check',type=Path,required=True)
        from verify_step_vision import verify
        return verify(ROOT,parser.parse_args().step_vision_check)
    if '--window-check' in sys.argv:
        # Strictly read-only: do not initialize/migrate config or roll the day.
        parser = argparse.ArgumentParser()
        parser.add_argument('--window-check', type=Path, required=True)
        args = parser.parse_args()
        from window_access import diagnostic
        return diagnostic(args.window_check.resolve())
    if '--worker' not in sys.argv:
        from assistant_lifetime import bind_assistant_lifetime
        bind_assistant_lifetime()
    from app_icon import initialize_app_identity
    initialize_app_identity()
    from local_cache import migrate_legacy_cache, ensure_local_defaults
    ensure_local_defaults(ROOT)
    migrate_legacy_cache(ROOT)
    from date_rollover import synchronize_date
    synchronize_date(ROOT)
    if '--worker' in sys.argv:
        index=sys.argv.index('--worker')
        return worker(sys.argv[index+1],sys.argv[index+2:])
    parser=argparse.ArgumentParser()
    parser.add_argument('--self-check',type=Path)
    parser.add_argument('--model-self-check', type=Path)
    parser.add_argument('--model-check-image', type=Path)
    parser.add_argument('--model-check-text')
    parser.add_argument('--model-check-hold', type=float, default=0)
    parser.add_argument('--open-process-log',type=Path,help='Open a retained local workflow log without running any task')
    parser.add_argument('--preview-output',type=Path)
    parser.add_argument('--preview-run',type=Path)
    parser.add_argument('--preview-process-output',type=Path)
    parser.add_argument('--preview-task',default='navigation',choices=[task[0] for task in TASKS])
    args=parser.parse_args()
    if args.preview_run and not args.preview_output:
        parser.error('--preview-run requires --preview-output')
    if args.model_self_check:
        from model_checks import check_embedded_model
        check_embedded_model(args.model_self_check, args.model_check_image, args.model_check_text, args.model_check_hold)
        return 0
    if args.self_check:
        args.self_check.mkdir(parents=True,exist_ok=True)
        self_check(args.self_check)
        return 0
    root=tk.Tk()
    from window_access import process_token
    try:
        low_integrity = process_token(os.getpid())['integrity_rid'] < 8192
    except Exception:
        low_integrity = False  # Worker preflight must still verify access.
    if low_integrity:
        messagebox.showwarning('启动权限不兼容',
            '当前助手以 Low 权限运行，不能正常访问普通权限游戏。\n'
            '请核对启动进程及 EXE 的完整性标签；目录继承的 Low 标签可能让正常双击也降权。\n'
            '任务将在窗口预检处停止；不会自动提权、修改 ACL 或重跑队列。', parent=root)
    from workbench_ui import WorkbenchApp
    app=WorkbenchApp(root)
    app.table.selection_set(args.preview_task)
    app.update_selection()
    if args.open_process_log:
        folder = args.open_process_log.resolve(strict=True)
        folder.relative_to((ROOT / 'evidence/workbench_runs').resolve())
        if not (folder / 'workflow.json').is_file():
            raise ValueError('未找到原队列记录，未执行任何任务。')
        app.output, app.running_task_id = folder, 'workflow'
        app.log_context = '原运行日志 · 热修后查看，未重跑任务'
        app.collect_receipts(final=True)
        app.open_process_window()
    if args.preview_run:
        app.table.selection_set(args.preview_task)
        app.execute.set(True)
        app.update_selection()
        app.status.set(worker_feedback(args.preview_run,args.preview_task))
    if args.preview_process_output:
        if not args.preview_output or not args.preview_run:
            parser.error('--preview-process-output requires --preview-output and --preview-run')
        from execution_log import LogEvent
        app.output, app.running_task_id = args.preview_run, args.preview_task
        app.log_context = '历史回放 · 不发送游戏输入'
        app.record_event(LogEvent('历史回放', 'warning', '以下是历史回执回放，接收时间为当前时间；未启动任务、未操作游戏。'))
        app.collect_receipts(final=True)
        app.open_process_window()
    if args.preview_output:
        def capture():
            from PIL import ImageGrab
            root.lift()
            root.attributes('-topmost',True)
            root.update()
            rect=(root.winfo_rootx(),root.winfo_rooty(),root.winfo_rootx()+root.winfo_width(),root.winfo_rooty()+root.winfo_height())
            args.preview_output.parent.mkdir(parents=True,exist_ok=True)
            ImageGrab.grab(bbox=rect).save(args.preview_output)
            if args.preview_process_output:
                window = app.process_view.window
                window.lift(); window.attributes('-topmost',True); window.update()
                time.sleep(.35); window.update()
                args.preview_process_output.parent.mkdir(parents=True,exist_ok=True)
                ImageGrab.grab(bbox=(window.winfo_rootx(),window.winfo_rooty(),window.winfo_rootx()+window.winfo_width(),window.winfo_rooty()+window.winfo_height())).save(args.preview_process_output)
            root.destroy()
        root.after(1200,capture)
    root.mainloop()
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception:
        # [异常出口] 步骤诊断失败只写调用者指定目录，非零退出避免冻结入口弹模态框。
        diagnostic_flag=next((flag for flag in ('--step-vision-self-check','--step-vision-check') if flag in sys.argv),None)
        if diagnostic_flag:
            failure=traceback.format_exc()
            try:
                output=Path(sys.argv[sys.argv.index(diagnostic_flag)+1]).resolve()
                output.mkdir(parents=True,exist_ok=True)
                (output/'step_vision_error.json').write_text(json.dumps({
                    'completed':False,'diagnostic':diagnostic_flag,'error':failure,
                    'game_input':False,'worker_started':False},ensure_ascii=False,indent=2),encoding='utf-8')
            except Exception:
                pass
            raise SystemExit(2)
        if '--window-check' in sys.argv:
            # A broken diagnostic must not fall back to a GUI or runtime writes.
            raise SystemExit(2)
        target=ROOT/'evidence/launcher_startup_error.txt'
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(traceback.format_exc(),encoding='utf-8')
        if '--self-check' not in sys.argv and '--worker' not in sys.argv:
            messagebox.showerror('棕色尘埃2辅助启动失败',f'错误记录：{target}\n{traceback.format_exc()}')
        raise
