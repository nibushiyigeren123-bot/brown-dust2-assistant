"""Date-scoped local progress. No game-account discovery or account identifiers."""
import hashlib
import json
import os
import shutil
from datetime import date, datetime
from pathlib import Path
from game_calendar import game_day


def ensure_local_defaults(root):
    root = Path(root)
    for path in (root / 'templates/config').glob('*.json'):
        target = root / 'config' / path.name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation never overwrites an existing local setting.
            try:
                with target.open('xb') as stream:
                    stream.write(path.read_bytes())
            except FileExistsError:
                pass


def cache_date(config):
    value = config.get('cache_date') or game_day().isoformat()
    date.fromisoformat(value)
    return value


def load_local_config(path):
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    if 'profile_id' in config or 'cycle_id' in config:
        raise RuntimeError('本地缓存需要升级，请先打开新版软件完成迁移；不会重置已有进度。')
    config['cache_date'] = cache_date(config)
    return config


def cache_path(root, config, module='daily'):
    day = cache_date(config)
    suffix = hashlib.sha256(module.encode('utf-8')).hexdigest()[:12]
    return Path(root) / 'runtime' / f'{day}_{suffix}.json'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def _legacy_day(data):
    # Read only the old date label. The old account label is never consulted.
    value = data.get('cache_date') or data.get('cycle_id')
    if not isinstance(value, str):
        raise RuntimeError('旧缓存没有可核对的日期；已保留原记录，不自动重置。')
    day = value[:10]
    date.fromisoformat(day)
    return day


def _clean(data):
    if isinstance(data, list):
        return [_clean(value) for value in data]
    if not isinstance(data, dict):
        return data
    # Remove obsolete fields without using their values as identity or keys.
    result = {key: _clean(data[key]) for key in data if key not in ('profile_id', 'cycle_id')}
    if 'cycle_id' in data:
        result['cache_date'] = _legacy_day(data)
    return result


def _module(data):
    if 'equipment' in data:
        return 'equipment'
    if 'event_id' in data:
        return 'event:' + data['event_id']
    if 'hunting' in data and 'goddess' in data and 'mirror' in data:
        return 'daily'
    if 'issued' in data:
        return 'guild'
    if 'checked_modes' in data:
        return 'daily_weekly_rewards'
    if 'checked_groups' in data:
        return 'pass_rewards'
    raise RuntimeError('存在无法归类的旧缓存；已保留原记录，请核对缓存类型。')


def migrate_legacy_cache(root):
    root = Path(root)
    # The same lock used by consuming workers also protects metadata migration.
    from pending_daily import exclusive_ledger
    with exclusive_ledger(root / 'runtime/game_control'):
        return _migrate_legacy_cache(root)


def _migrate_legacy_cache(root):
    """Upgrade metadata only, preserving progress. Caller must have no active worker.

    All conflicts and pending actions are checked before writing. Opaque backups
    remain local and are excluded from public exports. Interrupted copies can be
    retried because identical date/module destinations are accepted.
    """
    root = Path(root)
    configs, records, destinations = [], [], {}
    for path in (root / 'config').glob('*.json'):
        data = json.loads(path.read_text(encoding='utf-8'))
        if 'profile_id' in data or 'cycle_id' in data:
            configs.append((path, _clean(data)))
    for path in (root / 'runtime').glob('*.json'):
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('pending'):
            if configs or 'profile_id' in data or 'cycle_id' in data:
                raise RuntimeError('仍有未决操作，先核对原回执再迁移；已有缓存未重置。')
        if 'profile_id' not in data and 'cycle_id' not in data:
            continue
        cleaned = _clean(data)
        cleaned.setdefault('schema_version', 1)
        destination = cache_path(root, cleaned, _module(data))
        if destination in destinations and destinations[destination] != cleaned:
            raise RuntimeError('同一天存在多份不同进度缓存；请先选定要保留的一份，不自动合并。')
        if destination.exists() and json.loads(destination.read_text(encoding='utf-8')) != cleaned:
            raise RuntimeError('日期缓存与旧记录冲突，已保留两份原记录，不覆盖。')
        destinations[destination] = cleaned
        records.append((path, destination, cleaned))
    if not configs and not records:
        return None
    backup = root / 'runtime' / ('legacy_backup_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    for path, _ in configs:
        destination = backup / path.relative_to(root)
        destination.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(path, destination)
    for path, destination, cleaned in records:
        old = backup / path.relative_to(root)
        old.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(path, old)
        save(destination, cleaned)
        assert json.loads(destination.read_text(encoding='utf-8')) == cleaned
        if path != destination:
            path.unlink()
    for path, cleaned in configs:
        save(path, cleaned)
    result = {'format': 'local_date_cache', 'migrated_records': len(records),
              'config_files': len(configs), 'backup_directory': backup.name,
              'consuming_actions': 0}
    save(root / 'runtime/cache_migration.json', result)
    return result
