"""Event battle entry point with shared AP accounting and isolated execution."""
from frame_store import next_path, report_write, persist_references
import argparse
import json
from pathlib import Path
from daily_tasks import guard_foreign_pending, load_config as load_daily
from event_battle_vision import EventBattleVision
from event_reward_rules import event_ledger_path, load_config
from game_window import GameWindow
from pending_daily import exclusive_ledger
from project_paths import ROOT
from task_rewards import save


def inspect_cycle(config, ledger, observation):
    if ledger.get('pending'):
        return {'completed': False, 'pending': ledger['pending'], 'stopped': '活动存在未决消费，先核对原回执，不重新提交。'}
    if observation.get('state') != 'main_menu':
        return {'completed': False, 'stopped': '活动交接尚未确认主页；本次没有提交战斗。'}
    if ledger.get('completed') is True:
        return {'completed': True, 'returned_to_main_menu': True, 'already_completed': True,
                'spent_total': ledger.get('spent_total'), 'resource_pool': 'shared_event_ap'}
    if not config['event']['enabled']:
        return {'completed': True, 'returned_to_main_menu': True, 'skipped': True, 'skip_reason': '活动战斗在配置中停用；本次未消费。'}
    return {'completed': False, 'ready': True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--image', type=Path)
    parser.add_argument('--data-root', type=Path, default=ROOT)
    parser.add_argument('--prepare-only', action='store_true', help='只核对设置并取消返回，不提交战斗')
    parser.add_argument('--reconcile-pending', action='store_true', help='只回收已提交批次的结果，不重新开战')
    parser.add_argument('--entry-only',action='store_true',help='只核实解锁入口、进入活动中心并返回，不读取或写入业务账本')
    args = parser.parse_args()
    if args.image and args.execute:
        parser.error('保存的截图仅允许只读检查')
    if args.prepare_only and not args.execute:
        parser.error('设置预览包含导航，请显式使用 --execute --prepare-only')
    if args.reconcile_pending and (not args.execute or args.prepare_only):
        parser.error('回执恢复需要 --execute，且不能与设置预览同时使用')
    if args.entry_only and (not args.execute or args.prepare_only or args.reconcile_pending):
        parser.error('入口验收需要 --execute --entry-only，且不能与预览或回执恢复混用')
    args.output.mkdir(parents=True, exist_ok=True)
    if args.execute:
        from event_battle_runtime import run
        report = run(args)
    else:
        image_path = args.image
        if image_path is None:
            image, _ = GameWindow.find().capture()
            image_path = args.output / 'current.png'
            image_path=next_path(image_path);image.save(image_path)
        observation = EventBattleVision().inspect_file(image_path, args.output / 'recognition')
        report = {'execute': False, 'completed': False, 'actions': [],
                  'observations': [{'image': str(image_path), 'observation': observation}]}
        save(args.output / 'run.json', report)
    print(json.dumps({key: report.get(key) for key in ('execute', 'completed', 'prepared', 'error')}, ensure_ascii=False))
    if args.execute and not report['completed'] and not report.get('prepared'):
        raise SystemExit(1)



if __name__ == '__main__':
    main()
