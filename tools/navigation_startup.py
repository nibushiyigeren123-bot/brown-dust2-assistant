"""Finish at the verified menu; never append another module to navigation."""
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
    # Navigation ends at its verified visual goal. Module entry points own
    # date/pending checks, progress scans and explicit reward recovery.
    report.update(startup_stage='ready', startup_actions=[],
                  startup_scope='main_menu_only', returned_to_main_menu=True)
    save()
    print('已连续两帧确认主菜单；导航完成，推进下一队列目标。', flush=True)
