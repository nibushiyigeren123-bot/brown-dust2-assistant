"""Shared event journal adapter; unsupported consuming branches stop explicitly."""
import argparse
import json
from pathlib import Path
from daily_tasks import guard_foreign_pending, load_config as load_daily
from event_prepare_vision import EventPreparationVision
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
    return {'completed': False, 'stopped': '活动正常战斗与快速战斗通用执行链待接入；本次未消费。'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--image', type=Path)
    args = parser.parse_args()
    if args.image and args.execute:
        parser.error('保存的截图仅允许只读检查')
    args.output.mkdir(parents=True, exist_ok=True)
    config = load_config(ROOT / 'config/event_and_rewards.json')
    daily = load_daily(ROOT / 'config/daily_tasks.json')
    if any(config[key] != daily[key] for key in ('cache_date',)):
        raise RuntimeError('活动缓存日期与日常配置不一致。')
    path = event_ledger_path(ROOT, config)
    report = {'execute': args.execute, 'completed': False, 'actions': [], 'observations': [],
              'cache_date': config['cache_date'], 'ledger': str(path)}
    with exclusive_ledger(ROOT / 'runtime/game_control'):
        ledger = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        image_path = args.image
        if image_path is None:
            image, _ = GameWindow.find().capture()
            image_path = args.output / 'current.png'; image.save(image_path)
        observation = EventPreparationVision().inspect_file(image_path, args.output / 'recognition')
        report['observations'].append({'image': str(image_path), 'observation': observation})
        report['pending'] = ledger.get('pending')
        if args.execute:
            try:
                guard_foreign_pending(ROOT / 'runtime', path)
                report.update(inspect_cycle(config, ledger, observation))
            except Exception as error:
                report['stopped'] = str(error)
        save(args.output / 'run.json', report)
    if args.execute and not report['completed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
