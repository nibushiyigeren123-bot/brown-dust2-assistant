"""After verified navigation, inspect cache/pending work before any task action."""
import json
from pathlib import Path

from game_calendar import game_day


def finish_at_main_menu(root, output):
    root, output = Path(root), Path(output)
    path = output / 'run.json'
    report = json.loads(path.read_text(encoding='utf-8'))
    observations = report.get('observations') or []
    if (report.get('execute') is not True or report.get('completed') is not True
            or report.get('completion') != 'main_menu_observed_twice'
            or len(observations) < 2
            or any(item.get('state') != 'main_menu' for item in observations[-2:])):
        raise RuntimeError('尚未连续确认主菜单，不能开始核对后续命令。')
    report.update(navigation_completed=True, returned_to_main_menu=True,
                  cache_date=game_day().isoformat(), startup_stage='at_main_menu')
    def save():
        from atomic_json import write_json
        write_json(path, report)
    save()
    print('阶段1完成：已连续两帧确认主菜单。现在核对日期、缓存和待核实命令。', flush=True)
    audit_started = False
    try:
        from date_rollover import require_current_cache
        from daily_tasks import guard_foreign_pending, load_config
        require_current_cache(root)
        from task_rewards import ledger_path, reconcile_pending_claim
        from local_cache import load_local_config
        rewards_path = ledger_path(root, load_local_config(root / 'config/daily_tasks.json'))
        report['retained_pending'] = guard_foreign_pending(root / 'runtime', rewards_path, module='navigation')
        rewards = json.loads(rewards_path.read_text(encoding='utf-8')) if rewards_path.exists() else {}
        pending = rewards.get('pending')
        if pending and not (pending.get('module') == 'task_rewards'
                and pending.get('type') in ('claim_daily', 'claim_weekly')
                and pending.get('state') == pending['type'].split('_', 1)[1] + '_tasks'):
            guard_foreign_pending(root / 'runtime', root / 'runtime/launcher_nonledger.json', module='navigation')
        audit_started = True
        report.update(returned_to_main_menu=False, startup_stage='reconciling_rewards')
        save()
        recovery = reconcile_pending_claim(root, output / 'pending_recovery')
        if recovery:
            report['reward_pending_recovery'] = recovery
        guard_foreign_pending(root / 'runtime', root / 'runtime/launcher_nonledger.json', module='navigation')
        from hunting_probe import initial_task_audit
        audit_started = True
        report.update(returned_to_main_menu=False, startup_stage='checking_tasks')
        save()
        report['startup_task_audit'] = initial_task_audit(
            load_config(root / 'config/daily_tasks.json'), output / 'initial_task_check', root)
        report.update(returned_to_main_menu=True, startup_stage='ready')
    except Exception as error:
        prefix = '已回到主菜单；' if not audit_started else '已先确认主菜单；后续任务核对中断：'
        report.update(completed=False, error=prefix + str(error),
                      returned_to_main_menu=not audit_started, startup_stage='needs_review')
        raise RuntimeError(report['error']) from error
    finally:
        save()
