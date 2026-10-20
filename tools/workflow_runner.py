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
    from atomic_json import write_json
    write_json(path, value)


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
    if report.get('skipped') is True:
        return 'skipped'
    return 'verified'


def worker_command(task_id, output, execute):
    base = [sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, '-X', 'utf8', str(ROOT / 'tools/assistant_app.py')]
    args = ['--worker', task_id, '--output', str(output)]
    return base + args + (['--execute'] if execute else [])


def continuation_plan(state_path):
    from game_calendar import game_day
    path = Path(state_path).resolve(strict=True)
    path.relative_to((ROOT / 'evidence/workbench_runs').resolve())
    raw = path.read_bytes()
    previous = json.loads(raw.decode('utf-8'))
    if (previous['identity'].get('cache_date') != game_day().isoformat()
            or previous.get('traversed') or previous['status'] not in ('paused', 'stopped', 'needs_review')):
        raise RuntimeError('只能从当前游戏日的中断队列继续未运行项。')
    keys = [item['id'] for item in previous['tasks'] if item['status'] == 'queued' and item['id'] != 'navigation']
    if not keys:
        raise RuntimeError('没有尚未运行的项目；失败项不会自动重试。')
    return {'schema_version': 1, 'tasks': keys, 'selected': keys,
            'continuation': {'workflow': str(path), 'sha256': hashlib.sha256(raw).hexdigest(),
                             'execute': previous['execute']}}


def pending_receipts(root):
    """Read-only references for error summaries; never settle or relabel a ledger."""
    records = []
    for path in (root / 'runtime').glob('*.json'):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError('账本不是对象')
            pending = data.get('pending')
            if pending:
                records.append({'ledger': str(path), 'module': pending.get('module'),
                                'op_id': pending.get('op_id')})
        except (OSError, ValueError, AttributeError):
            # Unknown is still unresolved in the summary. Never hide it as empty.
            records.append({'ledger': str(path), 'module': '无法核对的记录',
                            'op_id': None, 'unreadable': True})
    return records


def final_summary(state):
    tasks = state['tasks'] + state.get('source_errors', [])
    summary = {name: sum(item['status'] in statuses for item in tasks)
               for name, statuses in {'success': ('verified', 'inspected'),
                    'failed': ('needs_review',), 'blocked': ('blocked',),
                    'skipped': ('skipped',), 'remaining': ('queued', 'running')}.items()}
    summary['errors'] = [dict(module=item['id'], reason=item.get('reason', '尚未核实完成。'),
        status=item['status'], output=item.get('output'), pending=item.get('pending', []),
        action='核对原运行记录和未决回执；本轮不会自动重试。')
        for item in tasks if item['status'] in ('needs_review', 'blocked')]
    summary['traversed'] = state.get('traversed', False)
    summary['control_reason'] = state.get('reason') if not state.get('traversed') else None
    return summary


def run(args):
    from execution_control import ExecutionControlError, read_control
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    state_path = output / 'workflow.json'
    identity, hashes = config_identity(ROOT)
    if args.resume:
        state = json.loads(state_path.read_text(encoding='utf-8'))
        if state['identity'] != identity or state['config_hashes'] != hashes or state['execute'] != args.execute:
            raise RuntimeError('缓存日期、配置或执行模式已改变，不能恢复旧队列。请仅选尚未运行项新建队列；旧失败及未决不会重试。')
        if state['status'] not in ('paused', 'stopped', 'needs_review') or state.get('traversed'):
            raise RuntimeError('当前队列不处于可恢复状态。')
        # Legacy failed/running attempts must not be replayed by the new policy.
        for item in state['tasks']:
            if item['status'] in ('running', 'needs_review', 'blocked'):
                item.update(status='needs_review' if item['status'] == 'running' else item['status'],
                            reason=item.get('reason', state.get('reason', '原尝试未核实完成；本轮不重试。')))
        state.update(status='running', completed=False, traversed=False)
        state.pop('reason', None)
        state.pop('stop_kind', None)
    else:
        plan = json.loads(args.manifest.read_text(encoding='utf-8')) if args.manifest else default_plan()
        tasks = validate_plan(plan)
        source_errors = []
        if plan.get('continuation'):
            source = plan['continuation']
            expected = continuation_plan(source['workflow'])
            if source != expected['continuation'] or tasks != expected['selected'] or source['execute'] != args.execute:
                raise RuntimeError('来源队列或未运行项已变化，未启动续跑；原证据保持。')
            previous = json.loads(Path(source['workflow']).read_text(encoding='utf-8'))
            source_errors = [dict(item, reason=item.get('reason') or previous.get('reason') or '原尝试未核实完成。',
                                  pending=pending_receipts(ROOT)) for item in previous['tasks'] + previous.get('source_errors', [])
                             if item['status'] in ('needs_review', 'blocked', 'running')]
            for item in source_errors:
                if item['status'] == 'running':
                    item['status'] = 'needs_review'
        if args.execute:
            tasks = ['navigation', *[key for key in tasks if key != 'navigation']]
        state = {'schema_version': 2, 'identity': identity, 'config_hashes': hashes,
                 'execute': args.execute, 'status': 'running', 'cursor': 0,
                 'tasks': [{'id': key, 'status': 'queued'} for key in tasks],
                 'completed': False, 'traversed': False,
                 'continuation': plan.get('continuation'), 'source_errors': source_errors}
    report = {'execute': args.execute, 'completed': False, 'actions': [], 'observations': [],
              **identity, 'workflow': str(state_path)}
    stop_path = Path(os.environ.get('BD2_STOP_FILE', str(output / 'stop.requested')))
    pause_path = Path(os.environ.get('BD2_PAUSE_FILE', str(output / 'pause.requested')))

    def verify_day():
        if args.execute:
            check_execution_day(identity['cache_date'])

    def verify_config():
        if config_identity(ROOT) != (identity, hashes):
            raise ExecutionControlError('运行中配置发生变化，已停止，未启动下一段。', 'configuration')

    save(state_path, state)
    try:
        with exclusive_ledger(ROOT / 'runtime/workflow_control'):
            for index in range(state['cursor'], len(state['tasks'])):
                item = state['tasks'][index]
                state['cursor'] = index
                if stop_path.is_file():
                    state.update(status='stopped', reason='已停止，未启动下一段。')
                    break
                if pause_path.is_file():
                    state.update(status='paused', reason='已在模块边界暂停。')
                    break
                verify_config()
                if item['id'] != 'navigation':
                    verify_day()
                if item['status'] != 'queued':
                    state['cursor'] = index + 1
                    continue
                key, module = item['id'], BY_ID[item['id']]
                step_output = output / f'{index:02d}_{key}'
                if step_output.exists() and not args.execute:
                    count = 1
                    while (output / f'{index:02d}_{key}_inspection_{count}').exists():
                        count += 1
                    step_output = output / f'{index:02d}_{key}_inspection_{count}'
                if step_output.exists():
                    # Unexpected evidence is not permission to rerun an input.
                    item.update(status='blocked', reason='存在原运行目录，未覆盖或重试；请核对原回执。', output=str(step_output))
                    state['cursor'] = index + 1
                    save(state_path, state)
                    continue
                step_output.mkdir()
                item.update(status='running', output=str(step_output), started_at=time.time())
                save(state_path, state)
                child, code = {}, None
                control_path = step_output / 'control.json'
                problem_path = step_output / 'item_problem.json'
                try:
                    command = worker_command(key, step_output, args.execute)
                    env = dict(os.environ, BD2_STOP_FILE=str(stop_path),
                               BD2_CONTROL_FILE=str(control_path), BD2_ITEM_PROBLEM_FILE=str(problem_path),
                               PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
                    env['BD2_DAILY_AUDIT_ID'] = state.get('daily_audit_id', '')
                    with (step_output / 'process.log').open('wb') as log:
                        process = subprocess.Popen(command, cwd=str(ROOT), env=env, stdout=log, stderr=log,
                                                   creationflags=subprocess.CREATE_NO_WINDOW)
                        item['pid'] = process.pid
                        save(state_path, state)
                        code = process.wait()
                    result_path = step_output / 'run.json'
                    if not result_path.is_file():
                        failure = step_output / 'worker_failure.json'
                        if failure.is_file():
                            result_path = failure
                        else:
                            raise RuntimeError(module.name + '未产生可核对的结果；退出码不能代替完成。')
                    child = json.loads(result_path.read_text(encoding='utf-8'))
                    if not isinstance(child, dict):
                        raise ValueError('模块回执不是对象。')
                    status = evaluate_result(child, code, args.execute)
                    if status == 'verified' and key == 'navigation':
                        audit = child.get('startup_task_audit') or {}
                        if audit.get('cache_date') != identity['cache_date'] or audit.get('completed') is not True or not audit.get('audit_id'):
                            status = 'needs_review'
                            child['stopped'] = '开始任务前的每日任务核对尚未完成。'
                        else:
                            state['daily_audit_id'] = audit['audit_id']
                    reason = child.get('stopped') or child.get('error')
                    if status == 'needs_review' and not reason:
                        reason = f'{module.name}执行器退出码：{code}，尚未核实完成。'
                    problem = json.loads(problem_path.read_text(encoding='utf-8')) if problem_path.is_file() else {}
                    if child.get('blocked') or problem.get('blocked'):
                        status, reason = 'blocked', problem.get('reason') or reason
                    item.update(status=status, exit_code=code, ended_at=time.time())
                    if reason:
                        item['reason'] = reason
                except ExecutionControlError:
                    raise
                except Exception as error:
                    item.update(status='needs_review', reason=str(error), exit_code=code, ended_at=time.time())
                # These checks are outside the recoverable per-module exception.
                control = read_control(control_path)
                if control:
                    raise ExecutionControlError(control['reason'], control['kind'])
                if stop_path.is_file():
                    raise ExecutionControlError('用户已停止当前任务。', 'stopped')
                verify_config()
                if key != 'navigation' or item['status'] in ('verified', 'skipped'):
                    verify_day()
                if item['status'] in ('needs_review', 'blocked'):
                    item['pending'] = pending_receipts(ROOT)
                    state['cursor'] = index + 1
                    save(state_path, state)
                    # The next real worker owns known navigation + two home frames.
                    continue
                observations = child.get('observations') or []
                latest = observations[-1] if observations else {}
                observation = latest.get('observation', latest)
                if not args.execute and key == 'navigation' and observation.get('state') != 'main_menu':
                    page = STATES.get(observation.get('state'), str(observation.get('state')))
                    state.update(status='needs_review', stop_kind='recognition',
                                 reason=f'已识别：{page}。只读模式不会返回主菜单，后续段暂未检查。')
                    # No game action was submitted: a later explicit resume may inspect again.
                    item['status'] = 'queued'
                    break
                if args.execute:
                    from game_window import GameWindow, check_requested_stop
                    from common_navigation import PageDiagnosis
                    from m00_recognize import read_image
                    from runtime_policy import network_wait_seconds, wait_observed
                    home_checks = 0
                    def observe_home():
                        nonlocal home_checks
                        image, _ = GameWindow.find().capture()
                        home = step_output / f'coordinator_home_check_{home_checks:03d}.png'
                        image.save(home)
                        home_checks += 1
                        return PageDiagnosis().inspect(read_image(home))
                    try:
                        wait_observed(observe_home, lambda obs: obs['state'] == 'main_menu',
                                      network_wait_seconds(), stop=check_requested_stop)
                        item['return_verified'] = True
                    except ExecutionControlError:
                        raise
                    except Exception as error:
                        item.update(status='needs_review', reason='返回主页未核实：' + str(error),
                                    pending=pending_receipts(ROOT))
                verify_day()
                state['cursor'] = index + 1
                save(state_path, state)
            else:
                verify_day()
                verify_config()
                state['traversed'] = True
                errors = any(item['status'] in ('needs_review', 'blocked') for item in state['tasks']) or bool(state.get('source_errors'))
                state['status'] = 'complete_with_errors' if errors else 'complete' if args.execute else 'inspected'
                state['completed'] = args.execute and not errors
    except Exception as error:
        for item in state['tasks']:
            if item['status'] == 'running':
                item.update(status='needs_review', ended_at=time.time(), reason=str(error))
        state.update(status='stopped' if isinstance(error, ExecutionControlError) and error.kind == 'stopped' else 'needs_review',
                     reason=str(error), stop_kind=getattr(error, 'kind', 'control'))
        report['error'] = str(error)
    finally:
        for item in state['tasks']:
            if item['status'] in ('needs_review', 'blocked'):
                item['pending'] = pending_receipts(ROOT)
        state['summary'] = final_summary(state)
        report.update(completed=state['completed'], traversed=state.get('traversed', False),
                      summary=state['summary'], status=state['status'], stop_kind=state.get('stop_kind'),
                      stopped=state.get('reason'),
                      returned_to_main_menu=bool(state['tasks'] and state['tasks'][-1].get('return_verified')))
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
    if result['status'] in ('needs_review', 'stopped', 'complete_with_errors') and not (
            not args.execute and result.get('stop_kind') == 'recognition'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
