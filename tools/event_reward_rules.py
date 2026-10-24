"""Prepared event batch accounting and explicit reward sweep coverage.

Pure rules only: this module neither clicks nor reports real-game acceptance.
"""
import json
import copy
import hashlib
from pathlib import Path
from local_cache import cache_path, load_local_config

from resource_pools import available_for_policy


def load_config(path):
    return validate_config(load_local_config(path))


def validate_config(config):
    # [兼容] 缺字段只在内存默认关闭；校验不修改调用方对象或私人配置文件。
    config = copy.deepcopy(config)
    enabled = config['rewards'].setdefault('character_pass_enabled', False)
    if type(enabled) is not bool:
        raise ValueError('是否有当期角色通行证必须是布尔值。')
    if config['schema_version'] != 1:
        raise ValueError('Unsupported schema')
    event=config['event']
    if event['mode'] not in ('normal','challenge'):
        raise ValueError('Unknown battle mode')
    for key in ('runs','ap_budget'):
        if type(event[key]) is not int or event[key] < 0:
            raise ValueError('Counts and budgets must be nonnegative integers')
    for key in ('enabled','use_owned_extra_ap','buy_ap','keep_existing_team'):
        if type(event[key]) is not bool:
            raise ValueError('Expected boolean setting')
    if event['buy_ap'] or not event['keep_existing_team']:
        raise ValueError('AP purchases and team edits are outside this adapter')
    method = event.get('battle_method', 'quick' if event.get('quick_battle', {}).get('enabled') else 'continuous')
    if method not in ('continuous', 'quick'):
        raise ValueError('活动方式请选择连续战斗或快速战斗。')
    if event['enabled'] and (event['runs'] <= 0 or event['ap_budget'] <= 0):
        raise ValueError('启用活动战斗前，请设置正数的次数和共享AP总额度。')
    if event['runs'] > 100:
        raise ValueError('单个活动计划最多100次。')
    if any(event[key]['enabled'] for key in ('story','event_scene')):
        raise ValueError('This extension has no verified execution adapter yet')
    if config['rewards']['buy_pass_or_levels'] or config['rewards']['mailbox']['enabled']:
        raise ValueError('Pass purchases and mailbox collection are not enabled')
    if config['guild']['enter_once_per_cycle'] is not True:
        raise ValueError('Guild entry is limited to one attempt per local cache date')
    return config


def guild_disabled_reason(config, task_ids, execute):
    if execute and 'guild' in task_ids and not config['guild']['enabled']:
        return '公会签到尚未启用。请打开“日常设置 → 公会签到”，勾选启用并保存后再执行；加入队列不等于启用模块。'
    return None


def event_ledger_path(data_root, config):
    # Normal and challenge share one activity AP pool. Mode is not an identity key.
    return cache_path(data_root, config, 'event:' + config['event']['event_id'])


def validate_event_submit(config, preparation, dialog, *, spent_total=0):
    event=config['event']
    if not event['enabled']:
        return {'allowed':False,'reason':'event_disabled_until_configured'}
    if preparation.get('state')!='event_preparation' or dialog.get('state')!='event_auto_dialog':
        return {'allowed':False,'reason':'missing_preparation_or_dialog'}
    if any(preparation.get(k) is None or preparation.get(k)!=dialog.get(k) for k in ('mode','stage')):
        return {'allowed':False,'reason':'stage_changed_or_unknown'}
    if dialog['mode']!=event['mode']:
        return {'allowed':False,'reason':'wrong_battle_mode'}
    unit,cost=preparation.get('unit_cost'),dialog.get('total_cost')
    if type(unit) is not int or unit<=0 or type(cost) is not int or cost<=0:
        return {'allowed':False,'reason':'cost_not_read'}
    if cost!=unit*event['runs'] or cost>event['ap_budget'] or event['runs']<=0:
        return {'allowed':False,'reason':'count_or_budget_mismatch'}
    if type(spent_total) is not int or spent_total < 0 or spent_total+cost > event['ap_budget']:
        return {'allowed':False,'reason':'shared_ap_budget_exceeded'}
    if dialog.get('free_only') is not (not event['use_owned_extra_ap']):
        return {'allowed':False,'reason':'resource_switch_mismatch'}
    free=dialog.get('free_ap')
    available=available_for_policy(free, dialog.get('owned_extra_ap'),
                                   not event['use_owned_extra_ap'], cost)
    if available is None or available<cost:
        return {'allowed':False,'reason':'owned_extra_pool_not_read' if event['use_owned_extra_ap'] else 'free_ap_insufficient_or_unknown'}
    return {'allowed':True,'quantity':event['runs'],'cost':cost,
            'mode':dialog['mode'],'start_stage':dialog['stage'],
            'resource_pool':'shared_event_ap','available_ap':available}


class EventBatchMonitor:
    """Only explicit unique victory screens count as successful clears."""
    def __init__(self,mode,start_stage,quantity,cost,ap_before):
        if mode not in ('normal','challenge') or any(type(v) is not int or v<=0 for v in (start_stage,quantity,cost)):
            raise ValueError('Invalid batch identity')
        self.mode,self.start,self.quantity,self.cost,self.ap_before=mode,start_stage,quantity,cost,ap_before
        self.victories=set()
        self.finished=False

    def observe(self,observation):
        state=observation['state']
        if self.finished:
            return {'status':'finished','successful':len(self.victories)}
        if state=='event_defeat':
            return {'status':'needs_review','successful':len(self.victories),'reason':'defeat_no_automatic_retry'}
        progress=observation.get('auto_progress')
        if progress and (progress['total']!=self.quantity or not 1<=progress['current']<=self.quantity):
            return {'status':'needs_review','reason':'batch_progress_mismatch'}
        if state=='event_victory':
            stage=observation.get('stage')
            if observation.get('mode')!=self.mode or type(stage) is not int or not self.start<=stage<self.start+self.quantity:
                return {'status':'needs_review','reason':'unexpected_victory_identity'}
            self.victories.add(stage)
            return {'status':'last_victory_return_pending' if len(self.victories)==self.quantity else 'wait',
                    'successful':len(self.victories)}
        if state=='event_preparation':
            if len(self.victories)!=self.quantity:
                return {'status':'needs_review','reason':'returned_with_unverified_successes','successful':len(self.victories)}
            if observation.get('mode')!=self.mode or observation.get('stage')!=self.start+self.quantity:
                return {'status':'needs_review','reason':'unexpected_return_stage'}
            if type(self.ap_before) is not int or observation.get('free_ap')!=self.ap_before-self.cost:
                return {'status':'needs_review','reason':'resource_delta_not_verified'}
            self.finished=True
            return {'status':'finished','successful':len(self.victories),'spent':self.cost}
        return {'status':'wait','successful':len(self.victories)}


class RewardSweep:
    """A red dot or reward popup cannot certify all tabs and milestones."""
    def __init__(self,config):
        settings=config['rewards']
        self.required={f'pass:{name}:{tab}' for name in settings['pass_order']
                       for tab in ('tasks','levels') if settings['pass_'+tab]}
        self.required.update(name for name in ('daily_task_rewards','daily_milestones',
                           'weekly_completed_task_rewards','weekly_milestones') if settings[name])
        if settings['daily_task_rewards'] or settings['daily_milestones']:
            self.required.update(('weekly_completed_task_rewards', 'weekly_milestones'))
        self.checked=set()

    def verify_empty(self,group,claimable,coverage_complete):
        if group not in self.required:
            raise ValueError('Unknown reward group')
        # Unknown is not the same as empty. The caller must verify the UI and
        # cover the relevant tab/scroll extent before marking a group checked.
        if claimable is False and coverage_complete is True:
            self.checked.add(group)
        else:
            self.checked.discard(group)
        return self.status()

    def invalidate_pass_levels(self,pass_id):
        self.checked.discard(f'pass:{pass_id}:levels')

    def status(self):
        return {'complete':self.checked==self.required,'remaining':sorted(self.required-self.checked)}
