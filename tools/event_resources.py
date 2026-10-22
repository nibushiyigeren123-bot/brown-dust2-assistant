"""Display observed event balances without treating the cache as a submit gate."""
import json
from pathlib import Path
from game_calendar import game_day


def retain_balance(root, observation, cache_date, event_id=None):
    if type(observation.get('free_ap')) is not int:
        return
    data = {'cache_date': cache_date, 'event_id': event_id or observation.get('event_id'), 'free_ap': observation['free_ap'],
            'free_ap_capacity': observation.get('free_ap_capacity'),
            'owned_extra_ap': observation.get('owned_extra_ap'),
            'source': observation.get('resource_source'), 'state': observation['state']}
    path = Path(root) / 'runtime/event_resources.json'
    from atomic_json import write_json
    if path.is_file() and json.loads(path.read_text(encoding='utf-8')) == data:
        return
    write_json(path, data)


def balance_text(root):
    try:
        data = json.loads((Path(root) / 'runtime/event_resources.json').read_text(encoding='utf-8'))
        if data.get('cache_date') != game_day().isoformat():
            raise ValueError('historical balance')
        free = data['free_ap']
        extra = data.get('owned_extra_ap')
        return f'最近画面：免费AP {free}；已有额外AP ' + (str(extra) if type(extra) is int else '未读到明确数量') + '。提交前会再次读取。'
    except (OSError, ValueError, KeyError):
        return 'AP余额：进入活动页面后自动读取；提交前重新核对。'
