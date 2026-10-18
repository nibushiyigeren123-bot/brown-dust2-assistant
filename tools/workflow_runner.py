"""Serial coordinator. Modules own game input and journals; this layer owns the queue."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from local_cache import cache_path, load_local_config

from module_catalog import BY_ID, default_plan, validate_plan
from pending_daily import exclusive_ledger
from project_paths import ROOT
from task_feedback import STATES
from game_calendar import check_execution_day


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def config_identity(root):
    daily = load_local_config(root / 'config/daily_tasks.json')
    event = load_local_config(root / 'config/event_and_rewards.json')
    keys = ('cache_date',)
    if any(not daily.get(k) or daily[k] != event.get(k) for k in keys):
        raise RuntimeError('数据缓存日期不一致，请先核对配置。')
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (root / 'config').glob('*.json') if p.name != 'workflow_plan.json'}
    return {k: daily[k] for k in keys}, hashes


def evaluate_result(report, exit_code, execute):
    if exit_code != 0 or report.get('error') or report.get('stopped'):
        return 'needs_review'
    if not execute:
        observations = report.get('observations') or []
        latest = observations[-1] if observations else {}
        observation = latest.get('observation', latest)
        return 'inspected' if observation.get('state') not in (None, 'unknown') else 'needs_review'
    if report.get('pending') or (report.get('progress') or {}).get('pending') or report.get('completed') is not True or report.get('module_completed') is False:
        return 'needs_review'
    if report.get('unavailable') == 'season_settlement':
        from mirror_status import settlement_from_report
        return 'skipped' if settlement_from_report(report, report.get('cache_date')) else 'needs_review'
    return 'verified'


def worker_command(task_id, output, execute):
    base = [sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, '-X', 'utf8', str(ROOT / 'tools/assistant_app.py')]
    args = ['--worker', task_id, '--output', str(output)]
    return base + args + (['--execute'] if execute else [])


def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    state_path = output / 'workflow.json'
    identity, hashes = config_identity(ROOT)
    if args.resume:
        state = json.loads(state_path.read_text(encoding='utf-8'))
        if state['identity'] != identity or state['config_hashes'] != hashes or state['execute'] != args.execute:
            raise RuntimeError('缓存日期、配置或执行模式已改变，不能恢复旧队列。')
        if state['status'] not in ('paused', 'stopped', 'needs_review'):
            raise RuntimeError('当前队列不处于可恢复状态。')
        state['status'] = 'running'
        state.pop('reason', None)
        state.pop('stop_kind', None)
    else:
        plan = json.loads(args.manifest.read_text(encoding='utf-8')) if args.manifest else default_plan()
        tasks = validate_plan(plan)
        if args.execute:
            tasks = ['navigation', *[key for key in tasks if key != 'navigation']]
        state = {'schema_version': 1, 'identity': identity, 'config_hashes': hashes,
                 'execute': args.execute, 'status': 'running', 'cursor': 0,
                 'tasks': [{'id': key, 'status': 'queued'} for key in tasks], 'completed': False}
    report = {'execute': args.execute, 'completed': False, 'actions': [], 'observations': [],
              **identity, 'workflow': str(state_path)}
    stop_path = Path(os.environ.get('BD2_STOP_FILE', str(output / 'stop.requested')))
    pause_path = Path(os.environ.get('BD2_PAUSE_FILE', str(output / 'pause.requested')))
    def verify_day():
        if args.execute:
            check_execution_day(identity['cache_date'])

    save(state_path, state)
    try:
        verify_day()
        with exclusive_ledger(ROOT / 'runtime/workflow_control'):
            for index in range(state['cursor'], len(state['tasks'])):
                verify_day()
                state['cursor'] = index
                if stop_path.is_file():
                    state['status'] = 'stopped'; state['reason'] = '已停止，未启动下一段。'
                    break
                if pause_path.is_file():
                    state['status'] = 'paused'; state['reason'] = '已在模块边界暂停。'
                    break
                if config_identity(ROOT) != (identity, hashes):
                    raise RuntimeError('运行中配置发生变化，已停止，未启动下一段。')
                item = state['tasks'][index]
                key = item['id']
                module = BY_ID[key]
                step_output = output / f'{index:02d}_{key}'
                if step_output.exists() and item['status'] != 'queued':
                    # Keep the previous attempt and its original pending/receipt evidence.
                    count = 1
                    while (output / f'{index:02d}_{key}_resume_{count}').exists():
                        count += 1
                    step_output = output / f'{index:02d}_{key}_resume_{count}'
                step_output.mkdir(parents=True, exist_ok=True)
                item.update(status='running', output=str(step_output), started_at=time.time())
                save(state_path, state)
                command = worker_command(key, step_output, args.execute)
                env = dict(os.environ, BD2_STOP_FILE=str(stop_path), PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
                env['BD2_DAILY_AUDIT_ID'] = state.get('daily_audit_id','')
                with (step_output / 'process.log').open('wb') as log:
                    process = subprocess.Popen(command, cwd=str(ROOT), env=env, stdout=log, stderr=log,
                                               creationflags=subprocess.CREATE_NO_WINDOW)
                    item['pid'] = process.pid
                    save(state_path, state)
                    # No forced termination: each module owns its pending-aware stop path.
                    code = process.wait()
                result_path = step_output / 'run.json'
                if not result_path.exists():
                    raise RuntimeError(module.name + '未产生可核对的结果；退出码不能代替完成。')
                child = json.loads(result_path.read_text(encoding='utf-8'))
                try:
                    verify_day()
                except RuntimeError:
                    item.update(status='needs_review', ended_at=time.time(), exit_code=code)
                    raise
                observations = child.get('observations') or []
                latest = observations[-1] if observations else {}
                observation = latest.get('observation', latest)
                has_observation = isinstance(observation.get('state'), str) and bool(observation['state'])
                status = evaluate_result(child, code, args.execute)
                if status == 'verified' and key == 'navigation':
                    audit=child.get('startup_task_audit') or {}
                    if audit.get('cache_date') != identity['cache_date'] or audit.get('completed') is not True or not audit.get('audit_id'):
                        status='needs_review'
                        child['stopped']='开始任务前的每日任务核对尚未完成。'
                    else:
                        state['daily_audit_id']=audit['audit_id']
                item.update(status=status, ended_at=time.time(), exit_code=code)
                if status == 'needs_review':
                    state['status'] = 'stopped' if stop_path.is_file() else 'needs_review'
                    state['reason'] = child.get('stopped') or child.get('error') or (
                        f'{module.name}执行器退出码：{code}，请核对运行记录。' if code else module.name + ('当前页面未识别，后续段暂未检查；只读检查不代表任务完成。' if not args.execute else '尚未确认完成。'))
                    if not args.execute and code == 0 and not child.get('error') and not child.get('stopped'):
                        if has_observation and state['status'] == 'needs_review':
                            state['stop_kind'] = 'recognition'
                        elif not has_observation:
                            state['reason'] = module.name + '未产生有效的页面识别回执，请核对运行记录。'
                    break
                if not args.execute and key == 'navigation':
                    if observation.get('state') != 'main_menu':
                        page = STATES.get(observation.get('state'), str(observation.get('state')))
                        state.update(status='needs_review', stop_kind='recognition',
                                     reason=f'已识别：{page}。只读模式不会关闭弹窗或进入主菜单，后续段暂未检查。'
                                            '若要进入主菜单，请选中“进入主菜单”，切换实机操作，再执行选中单段。')
                        # Recheck navigation on resume; do not skip an unready starting page.
                        break
                if args.execute:
                    from game_window import GameWindow
                    from common_navigation import PageDiagnosis
                    from m00_recognize import read_image
                    from runtime_policy import network_wait_seconds, wait_observed
                    from game_window import check_requested_stop
                    home_checks = 0
                    def observe_home():
                        nonlocal home_checks
                        image, _ = GameWindow.find().capture()
                        home = step_output / f'coordinator_home_check_{home_checks:03d}.png'; image.save(home)
                        home_checks += 1
                        return PageDiagnosis().inspect(read_image(home))
                    def waiting_home(elapsed, timeout):
                        state['message'] = f'{module.name}：等待主页响应 {elapsed:.0f}/{timeout:g} 秒，不重复操作'
                        save(state_path, state)
                    wait_observed(observe_home, lambda obs: obs['state'] == 'main_menu', network_wait_seconds(),
                                  on_wait=waiting_home, stop=check_requested_stop)
                    state.pop('message', None)
                    item['return_verified'] = True
                verify_day()
                state['cursor'] = index + 1
                save(state_path, state)
            else:
                verify_day()
                state['status'] = 'complete' if args.execute else 'inspected'
                state['completed'] = args.execute
                report['completed'] = args.execute
    except Exception as error:
        state.update(status='needs_review', reason=str(error))
        report['error'] = str(error)
    finally:
        report['stopped'] = state.get('reason') if state['status'] not in ('complete', 'inspected') else None
        report['stop_kind'] = state.get('stop_kind')
        report['completed'] = state['status'] == 'complete'
        report['returned_to_main_menu'] = report['completed']
        save(state_path, state)
        save(output / 'run.json', report)
    return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    result = run(args)
    print(json.dumps({'status': result['status'], 'completed': result['completed'],
                      'reason': result.get('reason')}, ensure_ascii=False))
    if result['status'] in ('needs_review', 'stopped') and not (
            not args.execute and result.get('stop_kind') == 'recognition'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
