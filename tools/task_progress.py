"""Read-only task progress, independent of window recognition and queue completion."""
import json
from pathlib import Path

from local_cache import cache_path, load_local_config
from project_paths import ROOT
from game_calendar import game_day
from mirror_status import settlement_from_report, verified_season_end

LABELS = {'completed':'已完成', 'incomplete':'未完成', 'unknown':'待核对', 'disabled':'未启用', 'ready':'起点就绪','unavailable':'结算中', 'season_end':'赛季末 · 已跳过', 'resource_end':'资源或预算不足 · 已结束'}


def latest_observation(report):
    record = report.get('observation')
    if not isinstance(record, dict):
        records = report.get('observations') or []
        record = records[-1] if records and isinstance(records[-1], dict) else {}
    return record.get('observation', record)


def result(state, reason, source='本次检查', day=None, historical=False):
    label = '本日已检查 · 仍会复查' if historical else LABELS[state]
    suffix = ' · 缓存' if source == '本地缓存' and not historical and state != 'season_end' else ''
    return {'state':state, 'label':label+suffix, 'reason':reason,
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
        if task_id == 'navigation' and report.get('navigation_completed') is True:
            observations = report.get('observations') or []
            if (report.get('cache_date') == game_day().isoformat() and len(observations) >= 2
                    and all(item.get('state') == 'main_menu' for item in observations[-2:])):
                progress = result('ready', '起点主菜单已连续两帧确认；后续命令按核对结果处理。', day=report['cache_date'])
                if report.get('error') or report.get('stopped'):
                    progress['label'] = '已回主菜单 · 待核实' if report.get('returned_to_main_menu') else '起点已确认 · 核对中断'
                    progress['reason'] = report.get('error') or report.get('stopped')
                return progress
        try:
            daily = load_local_config(self.root / 'config/daily_tasks.json')
            event = load_local_config(self.root / 'config/event_and_rewards.json')
        except (OSError, ValueError, RuntimeError, AttributeError, TypeError):
            return result('unknown', '配置无法读取，不能核对缓存日期。')
        day = daily['cache_date']
        if day != game_day().isoformat():
            return result('unknown', '这是历史缓存，不作为当前游戏日的完成依据；按北京时间08:00换日。', source='历史缓存', day=day)
        if report.get('cache_date') and report['cache_date'] != day:
            return result('unknown', '回执与当前缓存日期不一致。', day=day)
        scope = {'hunting':'daily', 'goddess_check_in':'daily', 'goddess_daily_reward':'daily', 'mirror':'daily',
                 'guild':'guild', 'equipment':'equipment', 'task_rewards':'daily_weekly_rewards', 'rewards':'pass_rewards', 'character_pass':'character_pass'}.get(task_id)
        # [合并进度] 角色开关严格布尔；角色未决必须先于普通完成记录阻断。
        character_enabled = event['rewards'].get('character_pass_enabled', False)
        if type(character_enabled) is not bool:
            return result('unknown', '角色通行证开关格式异常，请重新保存设置。', day=day)
        character_cache = None
        if task_id == 'character_pass' and not character_enabled:
            return result('disabled', '当期角色通行证在设置中关闭。', day=day)
        if task_id == 'rewards' and character_enabled:
            character_cache = self.read(cache_path(self.root, daily, 'character_pass'))
            if character_cache and (character_cache.get('_unreadable') or character_cache.get('cache_date') != day or character_cache.get('pending')):
                return result('unknown', '角色通行证记录未核实或存在未决，不能标记整项完成。', day=day)
        if task_id == 'event':
            if event['cache_date'] != day:
                return result('unknown', '活动和日常缓存日期不一致。', day=day)
            active_event_id = event['event']['event_id']
            if active_event_id in ('auto_current', 'configure_current_event'):
                legacy_id = active_event_id if active_event_id == 'configure_current_event' else None
                resources = self.read(self.root / 'runtime/event_resources.json') or {}
                active_event_id = (resources.get('event_id') if resources.get('cache_date') == day else None) or legacy_id
                report_id = (report.get('progress') or {}).get('event_id')
                if report_id:
                    if active_event_id not in (None, 'configure_current_event') and report_id != active_event_id:
                        return result('unknown', '本轮活动与当前缓存身份冲突，保留待核对。', day=day)
                    active_event_id = report_id
            scope = 'event:' + active_event_id if active_event_id else None
        cache = self.read(cache_path(self.root, daily, scope)) if scope else None
        if cache and (cache.get('_unreadable') or cache.get('cache_date') != day):
            return result('unknown', '本地记录暂不可读或日期不符，保留待核对。', day=day)
        if report.get('pending') or (report.get('progress') or {}).get('pending') or (cache and cache.get('pending')):
            return result('unknown', '存在未决操作，先核对回执，不重复执行。', day=day)
        if report.get('error') or report.get('stopped'):
            return result('unknown', '本轮已停止，完成情况须结合停止原因核对。', day=day)
        if task_id == 'mirror' and report.get('unavailable') == 'season_settlement':
            if settlement_from_report(report, day):
                return result('season_end', '已进入镜中场景并点击自动战斗，确认赛季结算提示后返回主菜单；跳过本段，战斗次数不变。', day=day)
            return result('unknown', '赛季末回执尚未完整核实，不能据此跳过或记作战斗完成。', day=day)
        # 回执充分的资源耗尽/无拍手返回仍保持任务未完成；不归类为设置停用。
        from daily_availability import normal_unavailable_reason
        unavailable_reason=normal_unavailable_reason(report)
        if unavailable_reason:
            # [进度] 正常结束不代表完成全部场次，显示资源原因并保留真实账本。
            if task_id == 'mirror' and report.get('unavailable') == 'mirror_resource_limit':
                return result('resource_end', unavailable_reason, day=day)
            # 原签到回执已确认时，仅签到项保持完成；未出现的拍手奖励仍未完成。
            # [合并] 一项签到到账不能代表合并行的所有启用互动已到账。
            confirmed_check_in=(task_id=='goddess_check_in' and set(daily['goddess']['actions'])<=set((report.get('progress') or {}).get('goddess',{}).get('done',[])))
            return result('completed' if confirmed_check_in else 'incomplete',unavailable_reason,day=day)
        if report.get('unavailable') in ('free_resources_exhausted','no_hand_after_check_in','no_available_interaction','mirror_resource_limit'):
            return result('unknown','正常跳过回执尚不完整，保留待核对。',day=day)
        if report.get('skipped'):
            return result('disabled', '本次因配置停用跳过，不记作任务完成。', day=day)
        obs = latest_observation(report)
        state = obs.get('state')
        # Positive current-frame evidence takes precedence over an earlier scan.
        if (task_id in ('rewards', 'character_pass', 'task_rewards')
                and not (task_id in ('rewards', 'character_pass') and cache and cache.get('action_policy') == 'direct_once')
                and state in ('daily_tasks','weekly_tasks','pass_tasks','pass_levels') and obs.get('claimable') is True):
            return result('incomplete', '当前页面有可领取项目，领奖检查尚未完成。', day=day)
        if task_id == 'free_draw' and state == 'gacha_page' and obs.get('free_available') is True:
            return result('incomplete', '当前类别仍有免费入口，服装与装备需分别核对。', day=day)
        if task_id == 'management' and state == 'management_panel' and obs.get('panel', {}).get('state') == 'enabled':
            return result('incomplete', '经营领取按钮可用，当前仍有待领取收益。', day=day)
        if task_id in ('goddess_check_in','goddess_daily_reward') and state == 'goddess_options':
            # [界面] 合并行检查所有启用动作；旧单项历史仍只检查原日奖。
            wanted=daily['goddess']['actions'] if task_id=='goddess_check_in' else ['daily_reward']
            if any(item.get('action') in wanted for item in obs.get('offers', []) if isinstance(item, dict)):
                return result('incomplete', '当前出现对应互动选项，尚未核对领取结果。', day=day)
        if report.get('module_completed') is False or report.get('deferred_actions'):
            return result('unknown', '部分互动尚未开放或未核实；返回主页不等于任务完成。', day=day)
        if report.get('verification_scope') == 'entry_only_no_interaction' or report.get('entry_completed'):
            return result('unknown', '仅验证了入口，尚未核对任务完成情况。', day=day)
        if task_id == 'navigation':
            return result('ready', '已确认主菜单；这仅表示导航起点就绪。', day=day) if state == 'main_menu' else result('unknown', '尚未确认主菜单。', day=day)
        if report.get('execute') is True and report.get('completed') is True and task_id not in ('equipment', 'event', 'hunting', 'mirror', 'guild', 'goddess_check_in', 'goddess_daily_reward', 'rewards', 'character_pass', 'task_rewards'):
            return result('completed', '本轮回执已核实所选任务范围完成。', day=day)
        if task_id == 'equipment':
            flags = (cache or {}).get('equipment') or report.get('progress') or {}
            keys = ('refine_completed','decompose_completed','filter_initialized')
            if all(flags.get(key) is True for key in keys):
                return result('completed', '精炼、单件分解及筛选初始化均已有确认记录。', '本地缓存', day)
            return result('unknown', '尚无三项完整完成证据；不把缺失记录当作游戏未完成。', day=day)
        if task_id == 'event' and cache:
            if cache.get('event_id') != active_event_id:
                return result('unknown', '活动记录身份不符，不能沿用完成状态。', day=day)
            counts = cache.get('runs_by_mode')
            if isinstance(counts, dict) and event['event']['enabled']:
                count = counts.get(event['event']['mode'], 0)
                target = event['event']['runs']
                if type(count) is not int or count < 0:
                    return result('unknown', '活动次数格式异常，保留待核对。', day=day)
                if target > 0 and count >= target:
                    return result('completed', f"所选难度已核实 {count}/{target} 次；普通／挑战累计已用 {cache.get('spent_total', '未知')} AP。", '本地缓存', day)
                if target > count:
                    return result('incomplete', f"所选难度已核实 {count}/{target} 次；共享AP消耗 {cache.get('spent_total', 0)}。", '本地缓存', day)
            elif cache.get('completed') is True:
                return result('completed', f"本活动已有完成批次，累计已用 {cache.get('spent_total', '未知')} AP；不重做。", '本地缓存', day)
        if task_id == 'guild' and cache and cache.get('completed') is True:
            if cache.get('issued') is True:
                return result('completed', '本缓存日期已有签到成功及返回回执。', '本地缓存', day)
            if cache.get('outcome') == 'visited_without_reward_receipt':
                return result('completed', '已稳定进入公会并返回主页；未观察到新奖励弹窗。', '本地缓存', day)
        if task_id in ('goddess_check_in','goddess_daily_reward'):
            # [进度] 同一真实账本服务合并行与旧日奖ID，不因单项done显示整段完成或建议重复领取。
            wanted=daily['goddess']['actions'] if task_id=='goddess_check_in' else ['daily_reward']
            progress = cache or report.get('progress') or {}
            done=set(progress.get('goddess', {}).get('done', []))
            if wanted and set(wanted)<=done:
                return result('completed', '所有启用互动已有已核实记录，不重复点击。', '本地缓存', day)
            if not daily['goddess']['enabled'] or not set(wanted)&set(daily['goddess']['actions']):
                return result('disabled', '对应女神像互动在设置中停用。', day=day)
        if task_id == 'hunting':
            hunting = (cache or report.get('progress') or {}).get('hunting', {})
            check = hunting.get('daily_check') or {}
            from hunting_probe import hunting_task_evidence
            current = hunting_task_evidence(obs)
            if current and current['status'] == 'not_done':
                if hunting.get('daily_done') or (check.get('status')=='done' and check.get('cache_date')==day):
                    return result('unknown', '当前每日任务0/1与缓存完成记录冲突，不重复狩猎。', day=day)
                return result('incomplete', '当前每日任务页明确显示狩猎0/1。', day=day)
            checked = bool(current and current['status']=='done') or (check.get('cache_date') == day and check.get('status') == 'done')
            done = checked or (hunting.get('daily_done') is True and hunting.get('runs',{}).get('daily',0) >= 1)
            plans = daily['hunting']['extra_plans']
            if done:
                from daily_tasks import hunt_target_key
                resources=self.read(self.root/'runtime/hunting_resources.json') or {}
                pools=resources.get('resources',{}) if resources.get('cache_date')==day else {}
                if not plans:
                    free=[pools.get(name,{}).get('free_available') for name in ('rice','torch')]
                    if any(type(value) is int and value>0 for value in free):
                        return result('incomplete','每日固定已完成；仍有免费体力。请指定额外目标，程序按页面余额刷取。','本地缓存',day)
                    if not all(type(value) is int and value==0 for value in free):
                        return result('unknown','每日固定已完成；不能据此认定免费体力用完，需进入页面读取米饭和火炬。',day=day)
                bindings = hunting.get('plan_targets', {})
                if any(plan['id'] in bindings and bindings[plan['id']] != hunt_target_key(plan) for plan in plans):
                    return result('unknown', '额外计划目标已改变，不能套用旧次数。', day=day)
                remaining = sum(max(0, plan['runs'] - hunting.get('runs', {}).get(plan['id'], 0)) for plan in plans)
                sweeps=[p for p in plans if p.get('runs_mode')=='all_free']
                if sweeps:
                    from daily_tasks import hunt_target_key
                    for plan in sweeps:
                        check=hunting.get('free_sweep_checks',{}).get(plan['id'],{})
                        expected_target=hunt_target_key(plan)
                        if plan.get('target_mode')=='lowest_stock':
                            from daily_tasks import STONE_STAGES
                            resolved=hunting.get('resolved_targets',{}).get(plan['id'],{})
                            inventory=resolved.get('inventory') or {}
                            if (set(inventory)!=set(STONE_STAGES)
                                    or any(type(v) is not int or v<0 for v in inventory.values())
                                    or resolved.get('stage')!=min(STONE_STAGES,key=lambda s:inventory[s])):
                                return result('unknown','自动最少圣石缺少完整库存与选关依据，保留待核对。',day=day)
                            expected_target=hunt_target_key({**plan,'stage':resolved['stage']})
                        free,unit=check.get('free_available'),check.get('unit_cost')
                        resource='torch' if plan['category']=='圣石洞穴' else 'rice'
                        current=pools.get(resource,{})
                        if current.get('observed_at','')>check.get('observed_at','') and type(current.get('free_available')) is int:
                            free=current['free_available']
                        if check.get('target')!=expected_target or check.get('cache_date')!=day or type(free) is not int or type(unit) is not int or unit<=0:
                            return result('unknown','每日固定已完成；用完免费计划仍需进入页面核对余额。',day=day)
                        if free>=unit:return result('incomplete','每日固定已完成；页面显示仍有可用于所选计划的免费体力。',day=day)
                    remaining=sum(max(0,p['runs']-hunting.get('runs',{}).get(p['id'],0)) for p in plans if p.get('runs_mode')!='all_free')
                if remaining:
                    return result('incomplete', f'固定日常已有完成依据，额外计划尚余 {remaining} 次。', '本地缓存', day)
                return result('completed', '固定日常和配置的额外计划均已满足。', '本地缓存' if cache else '设置确认', day)
            if check.get('cache_date') == day and check.get('status') == 'not_done':
                return result('incomplete', '每日任务页已自动确认0/1，固定狩猎尚未完成。', day=day)
            if not daily['hunting']['enabled']:
                return result('disabled', '狩猎模块在设置中停用。', day=day)
        if task_id == 'mirror':
            mirror = (cache or report.get('progress') or {}).get('mirror', {})
            if state not in ('mirror_auto_dialog', 'mirror_multiplier_dialog', 'mirror_battle', 'mirror_result', 'mirror_result_footer') and verified_season_end(mirror.get('availability'), day):
                return result('season_end', '本游戏日已实测自动战斗入口并确认赛季结算；已返回主菜单，战斗未完成且未消费。下次执行仍进入页面复查，换日后不沿用。', '本地缓存', day)
            count = (cache or report.get('progress') or {}).get('mirror', {}).get('completed')
            target = daily['mirror']['max_battles']
            if type(count) is int and target > 0 and count >= target:
                return result('completed', f'已核实 {count} 场，本次配置目标 {target} 场。', '本地缓存', day)
            if type(count) is int and count > 0 and count < target:
                return result('incomplete', f'已核实 {count}/{target} 场，尚未达到配置目标。', '本地缓存', day)
            if not daily['mirror']['enabled'] or target == 0:
                return result('disabled', '镜中之战在设置中停用或次数为零。', day=day)
        # [直接领取消费] 新策略只展示动作完成，不能沿用旧checked_groups宣称奖励已检查。
        if task_id in ('rewards', 'character_pass') and cache and cache.get('action_policy') == 'direct_once':
            tabs = [tab for tab in ('tasks', 'levels') if event['rewards']['pass_' + tab]]
            books = ('character',) if task_id == 'character_pass' else event['rewards']['pass_order']
            required = {f'{book}:{tab}' for book in books for tab in tabs}
            done = (cache.get('completed') is True and cache.get('action_completed') is True
                    and required <= set(cache.get('action_groups', [])))
            if done and task_id == 'rewards' and character_enabled:
                done = bool(character_cache and character_cache.get('action_policy') == 'direct_once'
                    and character_cache.get('completed') is True and character_cache.get('action_completed') is True
                    and {f'character:{tab}' for tab in tabs} <= set(character_cache.get('action_groups', [])))
            if done:
                progress = result('completed', '通行证一次领取操作已执行，奖励未核实；下次按设置重新执行。', '本地缓存', day, True)
                progress['label'] = '本日动作已执行 · 奖励未核实'
                return progress
            return result('unknown', '通行证一次领取操作尚未全部完成，奖励未核实。', day=day)
        # [角色进度] 旧记录继续保留原有解释，不覆盖或迁移历史账本。
        if task_id=='character_pass' and cache and cache.get('completed') is True:
            if {'character:tasks','character:levels'}<=set(cache.get('checked_groups',[])):
                return result('completed','本游戏日角色通行证可领奖励已检查；下次运行仍复查。','本地缓存',day,True)
        if task_id == 'rewards' and cache and cache.get('completed') is True:
            required = {f'{book}:{tab}' for book in event['rewards']['pass_order'] for tab in ('tasks','levels')}
            if required and required <= set(cache.get('checked_groups', [])):
                # [结果消费] 开启角色后，旧普通完成记录不能代替角色独立检查依据。
                if character_enabled and not (character_cache and character_cache.get('completed') is True
                        and {'character:tasks', 'character:levels'} <= set(character_cache.get('checked_groups', []))):
                    return result('unknown', '普通通行证已有检查记录；当期角色通行证仍待检查。', day=day)
                label = '普通及角色通行证' if character_enabled else '两本普通通行证'
                return result('completed', f'本游戏日已检查{label}可领奖励；本次仍会复查新增奖励。', '本地缓存', day, True)
        if task_id == 'task_rewards' and cache and cache.get('completed') is True:
            if {'daily','weekly'} <= set(cache.get('checked_modes', [])):
                return result('completed', '本游戏日每日及每周领奖扫描已完成；不代表所有任务均做完，每次仍会复查。', '本地缓存', day, True)
        if task_id == 'event' and not event['event']['enabled']:
            return result('disabled', '活动战斗在设置中停用，不记作完成。', day=day)
        return result('unknown', '已识别页面或尚无完成依据，仍需检查任务本身。', day=day)


def describe_progress(task_id, report=None, root=ROOT):
    return ProgressReader(root).assess(task_id, report)


def progress_text(progress):
    day = f"（{progress['cache_date']}）" if progress.get('cache_date') else ''
    return progress['label'] + day + '：' + progress['reason']
