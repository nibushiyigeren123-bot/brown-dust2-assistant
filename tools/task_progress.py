"""Read-only task progress, independent of window recognition and queue completion."""
import json
from pathlib import Path

from local_cache import cache_path, load_local_config
from project_paths import ROOT

LABELS = {'completed':'已完成', 'incomplete':'未完成', 'unknown':'待核对', 'disabled':'未启用', 'ready':'起点就绪'}


def latest_observation(report):
    record = report.get('observation')
    if not isinstance(record, dict):
        records = report.get('observations') or []
        record = records[-1] if records and isinstance(records[-1], dict) else {}
    return record.get('observation', record)


def result(state, reason, source='本次检查', day=None, historical=False):
    suffix = ' · 上次' if historical else ' · 缓存' if source == '本地缓存' else ''
    return {'state':state, 'label':LABELS[state]+suffix, 'reason':reason,
            'source':source, 'cache_date':day, 'historical':historical}


class ProgressReader:
    def __init__(self, root=ROOT):
        self.root, self.files = Path(root), {}

    def read(self, path):
        try:
            stat = path.stat(); signature = (stat.st_mtime_ns, stat.st_size)
            if path in self.files and self.files[path][0] == signature:
                return self.files[path][1]
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                raise ValueError('缓存不是对象')
            self.files[path] = (signature, value)
            return value
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {'_unreadable':True}

    def assess(self, task_id, report=None):
        try:
            return self._assess(task_id, report)
        except (KeyError, TypeError, ValueError, AttributeError):
            return result('unknown', '进度字段不完整或格式异常，保留待核对。')

    def _assess(self, task_id, report=None):
        report = report or {}
        try:
            daily = load_local_config(self.root / 'config/daily_tasks.json')
            event = load_local_config(self.root / 'config/event_and_rewards.json')
        except (OSError, ValueError, RuntimeError, AttributeError, TypeError):
            return result('unknown', '配置无法读取，不能核对缓存日期。')
        day = daily['cache_date']
        if report.get('cache_date') and report['cache_date'] != day:
            return result('unknown', '回执与当前缓存日期不一致。', day=day)
        scope = {'hunting':'daily', 'goddess_check_in':'daily', 'goddess_daily_reward':'daily', 'mirror':'daily',
                 'guild':'guild', 'equipment':'equipment', 'task_rewards':'daily_weekly_rewards', 'rewards':'pass_rewards'}.get(task_id)
        if task_id == 'event':
            if event['cache_date'] != day:
                return result('unknown', '活动和日常缓存日期不一致。', day=day)
            scope = 'event:' + event['event']['event_id']
        cache = self.read(cache_path(self.root, daily, scope)) if scope else None
        if cache and (cache.get('_unreadable') or cache.get('cache_date') != day):
            return result('unknown', '本地记录暂不可读或日期不符，保留待核对。', day=day)
        if report.get('pending') or (report.get('progress') or {}).get('pending') or (cache and cache.get('pending')):
            return result('unknown', '存在未决操作，先核对回执，不重复执行。', day=day)
        if report.get('error') or report.get('stopped'):
            return result('unknown', '本轮已停止，完成情况须结合停止原因核对。', day=day)
        if report.get('skipped'):
            return result('disabled', '本次因配置停用跳过，不记作任务完成。', day=day)
        obs = latest_observation(report)
        state = obs.get('state')
        # Positive current-frame evidence takes precedence over an earlier scan.
        if task_id in ('rewards', 'task_rewards') and state in ('daily_tasks','weekly_tasks','pass_tasks','pass_levels') and obs.get('claimable') is True:
            return result('incomplete', '当前页面有可领取项目，领奖检查尚未完成。', day=day)
        if task_id == 'free_draw' and state == 'gacha_page' and obs.get('free_available') is True:
            return result('incomplete', '当前类别仍有免费入口，服装与装备需分别核对。', day=day)
        if task_id == 'management' and state == 'management_panel' and obs.get('panel', {}).get('state') == 'enabled':
            return result('incomplete', '经营领取按钮可用，当前仍有待领取收益。', day=day)
        if task_id in ('goddess_check_in','goddess_daily_reward') and state == 'goddess_options':
            action = 'check_in' if task_id == 'goddess_check_in' else 'daily_reward'
            if any(item.get('action') == action for item in obs.get('offers', []) if isinstance(item, dict)):
                return result('incomplete', '当前出现对应互动选项，尚未核对领取结果。', day=day)
        if report.get('module_completed') is False or report.get('deferred_actions'):
            return result('unknown', '部分互动尚未开放或未核实；返回主页不等于任务完成。', day=day)
        if report.get('verification_scope') == 'entry_only_no_interaction' or report.get('entry_completed'):
            return result('unknown', '仅验证了入口，尚未核对任务完成情况。', day=day)
        if task_id == 'navigation':
            return result('ready', '已确认主菜单；这仅表示导航起点就绪。', day=day) if state == 'main_menu' else result('unknown', '尚未确认主菜单。', day=day)
        if report.get('execute') is True and report.get('completed') is True and task_id not in ('equipment', 'event', 'hunting', 'mirror', 'guild', 'goddess_check_in', 'goddess_daily_reward', 'rewards', 'task_rewards'):
            return result('completed', '本轮回执已核实所选任务范围完成。', day=day)
        if task_id == 'equipment':
            flags = (cache or {}).get('equipment') or report.get('progress') or {}
            keys = ('refine_completed','decompose_completed','filter_initialized')
            if all(flags.get(key) is True for key in keys):
                return result('completed', '精炼、单件分解及筛选初始化均已有确认记录。', '本地缓存', day)
            return result('unknown', '尚无三项完整完成证据；不把缺失记录当作游戏未完成。', day=day)
        if task_id == 'event' and cache and cache.get('completed') is True:
            if cache.get('event_id') == event['event']['event_id']:
                return result('completed', f"本活动已有完成批次，累计已用 {cache.get('spent_total', '未知')} AP；不重做。", '本地缓存', day)
            return result('unknown', '活动记录身份不符，不能沿用完成状态。', day=day)
        if task_id == 'guild' and cache and cache.get('completed') is True and cache.get('issued') is True:
            return result('completed', '本缓存日期已有签到成功及返回回执。', '本地缓存', day)
        if task_id in ('goddess_check_in','goddess_daily_reward'):
            action = 'check_in' if task_id == 'goddess_check_in' else 'daily_reward'
            progress = cache or report.get('progress') or {}
            if action in progress.get('goddess', {}).get('done', []):
                return result('completed', '已有对应互动的已核实记录。' + ('奖励可能再开放，本次仍需复查。' if action == 'daily_reward' else ''), '本地缓存', day, action == 'daily_reward')
            if not daily['goddess']['enabled'] or action not in daily['goddess']['actions']:
                return result('disabled', '对应女神像互动在设置中停用。', day=day)
        if task_id == 'hunting':
            hunting = (cache or report.get('progress') or {}).get('hunting', {})
            done = hunting.get('daily_done') is True or daily['hunting']['daily_task_status'] == 'done' or obs.get('daily_task_done') is True
            plans = daily['hunting']['extra_plans']
            if done:
                from daily_tasks import hunt_target_key
                bindings = hunting.get('plan_targets', {})
                if any(plan['id'] in bindings and bindings[plan['id']] != hunt_target_key(plan) for plan in plans):
                    return result('unknown', '额外计划目标已改变，不能套用旧次数。', day=day)
                remaining = sum(max(0, plan['runs'] - hunting.get('runs', {}).get(plan['id'], 0)) for plan in plans)
                if remaining:
                    return result('incomplete', f'固定日常已有完成依据，额外计划尚余 {remaining} 次。', '本地缓存', day)
                return result('completed', '固定日常和配置的额外计划均已满足。', '本地缓存' if cache else '设置确认', day)
            if daily['hunting']['daily_task_status'] == 'not_done' or obs.get('daily_task_done') is False:
                return result('incomplete', '设置确认或当前画面明确标记固定日常未完成。', day=day)
            if not daily['hunting']['enabled']:
                return result('disabled', '狩猎模块在设置中停用。', day=day)
        if task_id == 'mirror':
            count = (cache or report.get('progress') or {}).get('mirror', {}).get('completed')
            target = daily['mirror']['max_battles']
            if type(count) is int and target > 0 and count >= target:
                return result('completed', f'已核实 {count} 场，本次配置目标 {target} 场。', '本地缓存', day)
            if type(count) is int and count > 0 and count < target:
                return result('incomplete', f'已核实 {count}/{target} 场，尚未达到配置目标。', '本地缓存', day)
            if not daily['mirror']['enabled'] or target == 0:
                return result('disabled', '镜中之战在设置中停用或次数为零。', day=day)
        if task_id == 'rewards' and cache and cache.get('completed') is True:
            required = {f'{book}:{tab}' for book in event['rewards']['pass_order'] for tab in ('tasks','levels')}
            if required and required <= set(cache.get('checked_groups', [])):
                return result('completed', '上次已检查两本通行证四组；本次仍会复查新增奖励。', '本地缓存', day, True)
        if task_id == 'task_rewards' and cache and cache.get('completed') is True:
            if {'daily','weekly'} <= set(cache.get('checked_modes', [])):
                return result('completed', '上次每日及每周领奖扫描完成；不代表所有任务均做完，本次仍复查。', '本地缓存', day, True)
        if task_id == 'event' and not event['event']['enabled']:
            return result('disabled', '活动战斗在设置中停用，不记作完成。', day=day)
        return result('unknown', '已识别页面或尚无完成依据，仍需检查任务本身。', day=day)


def describe_progress(task_id, report=None, root=ROOT):
    return ProgressReader(root).assess(task_id, report)


def progress_text(progress):
    day = f"（{progress['cache_date']}）" if progress.get('cache_date') else ''
    return progress['label'] + day + '：' + progress['reason']
