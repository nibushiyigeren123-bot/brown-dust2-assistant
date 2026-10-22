"""Synchronize active configs only. Dated progress records are never relabelled."""
import copy
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from game_calendar import game_day, check_execution_day
from local_cache import load_local_config, save, initialize_day_cache

NAMES = ('daily_tasks.json', 'event_and_rewards.json')

def digest(data):
    return hashlib.sha256(data).hexdigest()

def journal_path(root):
    return Path(root) / 'runtime/date_rollover.json'

def read_journal(root):
    path = journal_path(root)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

def require_current_cache(root):
    if read_journal(root).get('status') == 'prepared':
        from execution_control import stop_execution
        stop_execution('日期切换尚未对账，暂不执行游戏操作。', 'configuration')
    daily, event = [load_local_config(Path(root) / 'config' / name) for name in NAMES]
    if daily['cache_date'] != event['cache_date']:
        from execution_control import stop_execution
        stop_execution('日常和活动缓存日期不一致，暂不执行。', 'configuration')
    current = game_day().isoformat()
    if daily['cache_date'] != current:
        from execution_control import stop_execution
        stop_execution(f"电脑当前游戏日为 {current}，活动配置日期仍为 {daily['cache_date']}；启动日期同步尚未完成。", 'configuration')
    initialize_day_cache(root, current)
    return current

def restore_prepared(root, record):
    backup = (Path(root) / record['backup']).resolve()
    if not backup.is_relative_to((Path(root) / 'runtime/date_rollover_backups').resolve()):
        raise RuntimeError('换日备份路径异常，保留现场待核对。')
    originals = {}
    for name in NAMES:
        old = (backup / name).read_bytes()
        current = (Path(root) / 'config' / name).read_bytes()
        if digest(old) != record['old_hashes'][name] or digest(current) not in (record['old_hashes'][name], record['new_hashes'][name]):
            raise RuntimeError('换日记录与配置冲突，保留现场，不自动覆盖。')
        originals[name] = old
    for name, value in originals.items():
        path = Path(root) / 'config' / name
        temporary = path.with_suffix('.rollover_restore.tmp')
        with temporary.open('wb') as stream:
            stream.write(value); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    record['status'] = 'rolled_back'
    save(journal_path(root), record)

def synchronize_date(root):
    root = Path(root)
    target = game_day().isoformat()
    previous = read_journal(root)
    values = [load_local_config(root / 'config' / name) for name in NAMES]
    raw = [json.loads((root / 'config' / name).read_text(encoding='utf-8')) for name in NAMES]
    if previous.get('status') != 'prepared' and all(v.get('cache_date') == target for v in raw):
        initialize_day_cache(root, target)
        return {'changed': False, 'cache_date': target}
    from pending_daily import exclusive_ledger
    with exclusive_ledger(root / 'runtime/workflow_control'), exclusive_ledger(root / 'runtime/game_control'):
        # Historical pending belongs to its own day. Calendar initialization
        # changes only active config dates, never the old operation or receipt.
        previous = read_journal(root)
        if previous.get('status') == 'prepared':
            restore_prepared(root, previous)
        target = game_day().isoformat()
        originals = {name: (root / 'config' / name).read_bytes() for name in NAMES}
        raw = [json.loads(originals[name].decode('utf-8')) for name in NAMES]
        values = [load_local_config(root / 'config' / name) for name in NAMES]
        if all(v.get('cache_date') == target for v in raw):
            initialize_day_cache(root, target)
            return {'changed': False, 'cache_date': target}
        # The clock is authoritative even if old config files disagree. Existing
        # per-day ledgers are retained, so selecting a date never resets progress.
        updated = copy.deepcopy(values)
        for value in updated:
            value['cache_date'] = target
        updated[0]['hunting']['daily_task_status'] = 'unknown'
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
        backup = root / 'runtime/date_rollover_backups' / stamp
        backup.mkdir(parents=True)
        for name, value in originals.items():
            (backup / name).write_bytes(value)
        encoded = {name: json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8') for name, value in zip(NAMES, updated)}
        record = {'schema_version': 1, 'status': 'prepared', 'from_dates': [v['cache_date'] for v in values],
                  'cache_date': target, 'reset_rule': 'Asia/Shanghai 08:00',
                  'backup': backup.relative_to(root).as_posix(),
                  'old_hashes': {name: digest(value) for name, value in originals.items()},
                  'new_hashes': {name: digest(value) for name, value in encoded.items()}}
        save(journal_path(root), record)
        try:
            check_execution_day(target)
            for name, value in zip(NAMES, updated):
                save(root / 'config' / name, value)
            check_execution_day(target)
        except Exception:
            restore_prepared(root, record)
            raise
        initialize_day_cache(root, target)
        record['status'] = 'complete'
        save(journal_path(root), record)
        save(backup / 'receipt.json', record)
        return {'changed': True, 'cache_date': target, 'from_dates': record['from_dates']}
