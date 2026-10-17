"""Prune only dated local progress caches, keeping today, yesterday and protected data."""
import json
import re
import stat
from datetime import date, timedelta
from pathlib import Path

from local_cache import cache_path, load_local_config, save
from pending_daily import exclusive_ledger

CACHE_NAME = re.compile(r'^(\d{4}-\d{2}-\d{2})_[0-9a-f]{12}\.json$')


def automatic_cleanup_enabled(root):
    path = Path(root) / 'config/runtime_preferences.json'
    if not path.exists():
        return True
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return isinstance(value, dict) and value.get('auto_clear_old_cache', True) is True
    except (OSError, ValueError):
        return False


def set_automatic_cleanup(root, enabled):
    path = Path(root) / 'config/runtime_preferences.json'
    with exclusive_ledger(Path(root) / 'runtime/game_control'):
        value = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if not isinstance(value, dict):
            raise ValueError('缓存维护设置格式异常，保留原文件。')
        value['auto_clear_old_cache'] = bool(enabled)
        save(path, value)


def protected_dates(root):
    # Invalid configuration stops cleanup instead of silently dropping protection.
    return {load_local_config(Path(root) / 'config' / name)['cache_date']
            for name in ('daily_tasks.json', 'event_and_rewards.json')}


def plan_cleanup(root, today=None):
    root = Path(root)
    today = today or date.today()
    cutoff = today - timedelta(days=1)
    result = {'today':today.isoformat(), 'keep_since':cutoff.isoformat(), 'candidates':[], 'kept':[], 'errors':[]}
    try:
        protected = protected_dates(root)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError):
        result['errors'].append('配置日期无法核对，未清理任何缓存。')
        return result
    runtime_path = root / 'runtime'
    runtime = runtime_path.resolve()
    if runtime_path.exists() and runtime_path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        result['errors'].append('runtime 指向其他目录，未清理任何缓存。')
        return result
    for path in sorted((root / 'runtime').glob('*.json')):
        match = CACHE_NAME.fullmatch(path.name)
        if not match:
            continue  # Never touch logs, recovery pointers, backups or arbitrary files.
        reason = None
        try:
            if path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT or path.resolve().parent != runtime:
                reason = '路径不是本地普通缓存文件'
            else:
                day = date.fromisoformat(match.group(1))
                data = json.loads(path.read_text(encoding='utf-8'))
                version_valid = type(data.get('schema_version')) is int and data['schema_version'] == 1
                # Earlier pass scans used the same date/module name without an
                # explicit schema field. Recognize only that exact legacy shape.
                legacy_pass = (data.get('schema_version') is None and isinstance(data.get('checked_groups'), list)
                               and path.name == cache_path(root, data, 'pass_rewards').name)
                if data.get('cache_date') != day.isoformat() or not (version_valid or legacy_pass):
                    reason = '日期或格式无法核对'
                elif data.get('pending'):
                    reason = '有未决操作'
                elif day.isoformat() in protected:
                    reason = '当前选用的缓存日期'
                elif day >= cutoff:
                    reason = '保留今天和昨天（以及未来日期）'
        except (OSError, ValueError, TypeError, AttributeError):
            reason = '文件暂不可读'
        item = {'file':path.name, 'date':match.group(1)}
        if reason:
            result['kept'].append(item | {'reason':reason})
        else:
            result['candidates'].append(item)
    return result


def clear_old_cache(root, today_provider=date.today):
    root = Path(root)
    result = {'deleted':[], 'kept':[], 'errors':[], 'busy':False}
    try:
        runtime_path = root / 'runtime'
        if runtime_path.exists() and runtime_path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            result['errors'].append('runtime 指向其他目录，未清理任何缓存。')
            return result
        with exclusive_ledger(root / 'runtime/workflow_control'), exclusive_ledger(root / 'runtime/game_control'):
            today = today_provider()
            plan = plan_cleanup(root, today)
            result.update(today=plan['today'], keep_since=plan['keep_since'], kept=plan['kept'], errors=plan['errors'])
            if result['errors']:
                return result
            runtime = (root / 'runtime').resolve()
            for item in plan['candidates']:
                # Revalidate immediately before removal. A changed date/config or
                # pending record must invalidate the earlier preview.
                if today_provider() != today:
                    result['errors'].append('系统日期发生变化，本次清理已停止。')
                    break
                fresh = plan_cleanup(root, today)
                if fresh['errors']:
                    result['errors'].extend(fresh['errors']); break
                if item['file'] not in {candidate['file'] for candidate in fresh['candidates']}:
                    result['kept'].append(item | {'reason':'清理前保护条件已变化'})
                    continue
                path = root / 'runtime' / item['file']
                if path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT or path.resolve().parent != runtime:
                    result['errors'].append('缓存路径发生变化，未删除。'); break
                try:
                    path.unlink()
                    result['deleted'].append(item)
                except OSError:
                    result['errors'].append('缓存文件无法删除：' + item['file'])
            if result['deleted'] or result['errors']:
                save(root / 'runtime/cache_cleanup.json', result)
    except (OSError, RuntimeError):
        result['busy'] = True
        result['errors'].append('执行器占用缓存，本次清理暂缓。')
    return result


def cleanup_summary(result):
    if result.get('busy'):
        return '缓存正在使用，本次清理暂缓。'
    return f"旧缓存清理：清除 {len(result.get('deleted', []))} 项，保留 {len(result.get('kept', []))} 项。" + (' ' + '；'.join(result.get('errors', [])) if result.get('errors') else '')
