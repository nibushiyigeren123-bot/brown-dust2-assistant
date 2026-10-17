"""Portable GUI and isolated worker entry points for the prepared modules."""
import argparse
import importlib
import json
import os
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


from module_catalog import task_rows
TASKS = task_rows()


def worker(task_id, arguments):
    task = next((t for t in TASKS if t[0] == task_id), None)
    if task is None or task[4] is None:
        raise ValueError('该功能尚无可执行模块')
    output = Path(arguments[arguments.index('--output')+1]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Windowed executables have no stdout by default. Keep a real UTF-8 log.
    log = (output/'worker.log').open('a', encoding='utf-8', buffering=1)
    sys.stdout = sys.stderr = log
    sys.argv = [sys.argv[0], *task[5], *arguments]
    code = 0
    try:
        module = importlib.import_module(task[4])
        if task[0] in ('navigation','management','free_draw'):
            from daily_tasks import guard_foreign_pending
            from pending_daily import exclusive_ledger
            with exclusive_ledger(ROOT/'runtime/game_control'):
                if '--execute' in arguments:
                    guard_foreign_pending(ROOT/'runtime',ROOT/'runtime/launcher_nonledger.json')
                module.main()
        else:
            module.main()
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else 1
    except Exception:
        traceback.print_exc()
        code = 1
    finally:
        (output/'worker_result.json').write_text(json.dumps({'task':task_id, 'exit_code':code,
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
    hunt_config=copy.deepcopy(load_config(ROOT/'config/daily_tasks.json'))
    load_event(ROOT/'config/event_and_rewards.json')
    assert (ROOT/'tools/windows_ocr.ps1').is_file()
    if not (ROOT/'assets/launcher/sample_menu.jpg').is_file():
        # Public distributions omit personal full-screen acceptance fixtures.
        # This checks loaders/config/rules only; saved-image acceptance is separate.
        from equipment_vision import EquipmentVision
        from task_reward_vision import TaskRewardVision
        from pass_reward_vision import PassRewardVision
        from event_prepare_vision import EventPreparationVision
        from module_catalog import default_plan, validate_plan
        from equipment_rules import decompose_candidate_allowed
        for cls in (EquipmentVision, TaskRewardVision, PassRewardVision, EventPreparationVision):
            cls()
        assert len(validate_plan(default_plan())) == 12
        card = {'quality':'R', 'locked':False, 'equipped':False}
        assert decompose_candidate_allowed(card, ['R']) and not decompose_candidate_allowed(card, ['N', 'R'])
        report = {'frozen': bool(getattr(sys, 'frozen', False)), 'template_loaders': 9,
                  'workbench_registry_loaded': True, 'config_validated': True,
                  'equipment_rules_single_quality': True, 'personal_fixtures_shipped': False,
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
    from module_catalog import default_plan, validate_plan
    PassRewardVision()
    assert len(validate_plan(default_plan())) == 12
    report={'workbench_registry_loaded':True,'pass_reward_recognizer_loaded':True,'task_reward_recognizer_loaded':True,'frozen':bool(getattr(sys,'frozen',False)),'data_root':str(ROOT),
            'template_loaders':5,'menu_sample':'main_menu','guild_sample':receipt['state'],
            'native_windows_ocr':True,'free_draw_skip_recognized':True,'guild_live_receipt_recognized':True,
            'hunting_stones_inventory_recognized':True,'hunting_single_price_recognized':True,
            'hunting_middle_difficulty_recognized':True,'mirror_one_multiplier_and_price_recognized':True,
            'mirror_ready_recognized':True,'equipment_saved_R_recognized':True,'equipment_result_recognized':True,
            'equipment_default_inventory':904,'equipment_rules_single_quality':True,'game_input':False}
    (output/'self_check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


def main():
    from app_icon import initialize_app_identity
    initialize_app_identity()
    from local_cache import migrate_legacy_cache, ensure_local_defaults
    ensure_local_defaults(ROOT)
    migrate_legacy_cache(ROOT)
    if '--worker' in sys.argv:
        index=sys.argv.index('--worker')
        return worker(sys.argv[index+1],sys.argv[index+2:])
    parser=argparse.ArgumentParser()
    parser.add_argument('--self-check',type=Path)
    parser.add_argument('--preview-output',type=Path)
    parser.add_argument('--preview-run',type=Path)
    parser.add_argument('--preview-process-output',type=Path)
    parser.add_argument('--preview-task',default='navigation',choices=[task[0] for task in TASKS])
    args=parser.parse_args()
    if args.preview_run and not args.preview_output:
        parser.error('--preview-run requires --preview-output')
    if args.self_check:
        args.self_check.mkdir(parents=True,exist_ok=True)
        self_check(args.self_check)
        return 0
    root=tk.Tk()
    from workbench_ui import WorkbenchApp
    app=WorkbenchApp(root)
    app.table.selection_set(args.preview_task)
    app.update_selection()
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
        target=ROOT/'evidence/launcher_startup_error.txt'
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(traceback.format_exc(),encoding='utf-8')
        if '--self-check' not in sys.argv and '--worker' not in sys.argv:
            messagebox.showerror('棕色尘埃2辅助启动失败',f'错误记录：{target}\n{traceback.format_exc()}')
        raise
