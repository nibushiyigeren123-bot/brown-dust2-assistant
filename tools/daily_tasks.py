"""Configured daily modules with persisted pending actions and budget checks.

All coordinates in decisions use a normalized 1280x720 client. Nothing in this
module sends input. pending_daily.py is the explicit live execution entry point.
"""
import hashlib
import json
import os
import time
from datetime import date
from pathlib import Path
from local_cache import cache_path, load_local_config

from daily_vision import compact

STONE_STAGES = ('火之洞穴', '水之洞穴', '风之洞穴', '光之洞穴', '暗之洞穴')
HUNT_DIFFICULTIES = ('中游', '困难', '极难')


def hunt_free_only(settings, plan):
    # Read fallback without mutating legacy config or its pending-action hash.
    return plan.get('free_only', settings['free_only'])


def hunt_target_key(plan):
    target = [plan['category'], plan.get('stage'), plan['difficulty']]
    if plan.get('target_mode', 'specified') != 'specified':
        target.append(plan['target_mode'])
    return target


def load_config(path):
    config = load_local_config(path)
    return validate_config(config)


def validate_config(config):
    if config.get('schema_version') != 1:
        raise ValueError('Unsupported configuration schema')
    date.fromisoformat(config['cache_date'])
    hunt, goddess, mirror = (config[k] for k in ('hunting', 'goddess', 'mirror'))
    date.fromisoformat(config['execute_not_before'])
    for settings, fields in ((hunt, ('enabled','free_only')), (goddess, ('enabled','user_positioned_near_statue')),
                             (mirror, ('enabled','free_only','auto_skip','keep_existing_team'))):
        if any(type(settings[field]) is not bool for field in fields):
            raise ValueError('Switch values must be true or false')
    if type(hunt['daily_runs']) is not int or hunt['daily_runs'] != 1:
        raise ValueError('The fixed hunting daily must remain exactly one run')
    if hunt['daily_target']['category'] != '狩猎场':
        raise ValueError('The fixed daily must use the hunting ground category')
    if hunt['daily_target'].get('stage') not in (None,'野猪洞穴'):
        raise ValueError('固定日常必须使用关卡1：野猪洞穴')
    if hunt['daily_task_status'] not in ('unknown', 'not_done', 'done'):
        raise ValueError('Invalid hunting daily task status')
    if any(v is not None and (type(v) is not int or v < 0) for v in hunt['budgets'].values()):
        raise ValueError('Resource budgets must be nonnegative integers or null')
    if not mirror['keep_existing_team']:
        raise ValueError('Automatic team editing is not supported')
    for key in ('max_battles', 'resource_budget', 'resource_multiplier'):
        if type(mirror[key]) is not int or mirror[key] < (1 if key == 'resource_multiplier' else 0):
            raise ValueError(f'Invalid mirror {key}')
    if mirror['resource_multiplier'] > 40:
        raise ValueError('The observed mirror multiplier control supports 1 to 40')
    if len(set(goddess['actions'])) != len(goddess['actions']) or not set(goddess['actions']) <= {'check_in', 'daily_reward'}:
        raise ValueError('Invalid goddess actions')
    plans = hunt['extra_plans']
    if len({p['id'] for p in plans}) != len(plans) or any(p['id'] == 'daily' for p in plans):
        raise ValueError('Extra hunting plans require distinct non-daily IDs')
    for plan in plans:
        if type(plan['runs']) is not int or plan['runs'] < 0:
            raise ValueError('Invalid extra hunting run count')
    for plan in [hunt['daily_target'], *plans]:
        if 'free_only' in plan and type(plan['free_only']) is not bool:
            raise ValueError('Each hunting free-only switch must be true or false')
        if plan['category'] not in ('狩猎场', '金币', '史莱姆', '圣石洞穴'):
            raise ValueError('Unsupported hunting category')
        if plan['difficulty'] not in HUNT_DIFFICULTIES:
            raise ValueError('狩猎难度请选择中游、困难或极难')
        mode = plan.get('target_mode', 'specified')
        if mode not in ('specified', 'lowest_stock') or (mode == 'lowest_stock' and plan['category'] != '圣石洞穴'):
            raise ValueError('Lowest-stock selection is supported only for holy stones')
        if mode=='specified' and plan['category']=='圣石洞穴' and plan.get('stage') not in STONE_STAGES:
            raise ValueError('圣石目标请选择火、水、风、光、暗的洞穴')
        if mode=='specified' and plan is not hunt['daily_target'] and not plan.get('stage'):
            raise ValueError('每条额外狩猎计划都需要填写关卡')
    return config


def guard_foreign_pending(directory, selected_path):
    """One visible game client cannot be reset by selecting a different ledger."""
    selected_path = Path(selected_path).resolve()
    for path in Path(directory).glob('*.json'):
        if path.resolve() == selected_path:
            continue
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('schema_version') == 1 and data.get('pending'):
            raise RuntimeError(f'An unresolved action exists in another local progress cache: {path}')


class DailyLedger:
    def __init__(self, path, config):
        self.path = Path(path).resolve() if path else None
        self.config = config
        self.config_hash = hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
        if self.path and self.path.exists():
            self.data = json.loads(self.path.read_text(encoding='utf-8'))
            if self.data['cache_date'] != config['cache_date']:
                raise RuntimeError('Local cache date mismatch; do not reset existing progress')
            if self.data.get('pending') and self.data['pending']['config_hash'] != self.config_hash:
                raise RuntimeError('Configuration changed while an action is pending; reconcile it first')
        else:
            self.data = {'schema_version': 1, 'cache_date': config['cache_date'],
                         'pending': None, 'last_settled': None,
                         'hunting': {'daily_done': False, 'runs': {}, 'spent': {'rice': 0, 'torch': 0}},
                         'goddess': {'done': []}, 'mirror': {'completed': 0, 'spent': 0}, 'events': []}
        bindings = self.data['hunting'].setdefault('plan_targets', {})
        for plan in config['hunting']['extra_plans']:
            target = hunt_target_key(plan)
            if plan['id'] in bindings and bindings[plan['id']] != target:
                raise RuntimeError('An existing hunting plan ID changed target; create a new plan ID')

    def save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + '.tmp')
            with temporary.open('w', encoding='utf-8', newline='\n') as handle:
                handle.write(json.dumps(self.data, ensure_ascii=False, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)

    def submit_intent(self, action):
        if self.data['pending']:
            raise RuntimeError('Cannot submit another action while one is pending')
        self.data['pending'] = {**action, 'config_hash': self.config_hash, 'input_receipt': None}
        if action['module'] == 'hunting' and action['plan_id'] != 'daily':
            plan = next(p for p in self.config['hunting']['extra_plans'] if p['id'] == action['plan_id'])
            self.data['hunting']['plan_targets'][plan['id']] = hunt_target_key(plan)
        self.save()  # Persist before the mouse press, including the crash window.

    def settle(self, evidence):
        pending = self.data['pending']
        if not pending:
            raise RuntimeError('No pending action to settle')
        module = pending['module']
        if module == 'hunting':
            state = self.data['hunting']
            state['runs'][pending['plan_id']] = state['runs'].get(pending['plan_id'], 0) + pending['quantity']
            state['spent'][pending['resource']] += pending['cost']
            if pending['plan_id'] == 'daily':
                state['daily_done'] = True
        elif module == 'goddess':
            self.data['goddess']['done'].append(pending['task'])
        elif module == 'mirror':
            self.data['mirror']['completed'] += pending['quantity']
            self.data['mirror']['spent'] += pending['cost']
        self.data['last_settled'] = {'module': module, 'task': pending.get('task'),
                                     'quantity': pending.get('quantity'), 'evidence': evidence}
        self.data['events'].append({'action': pending, 'result': evidence})
        self.data['pending'] = None
        self.save()


class DailyController:
    def __init__(self, config, ledger, module, checkpoint='available'):
        self.config, self.ledger, self.module = config, ledger, module
        self.checkpoint = checkpoint
        self.last_ready = None
        self.ready_observations = 0
        self.adjustments = 0
        self.cleanup_clicks = 0
        self.cleanup_event = None
        self.selected_hunt_category = None
        self.selected_hunt_stage = None
        self.difficulty_changes = 0
        self.active_hunt_plan = None
        self.goddess_scene_attempts = 0
        self.goddess_scene_transition_since = None
        self.goddess_collection_attempts = 0
        self.goddess_collection_transition_since = None
        self.mirror_collection_attempts = 0
        self.mirror_card_attempts = 0
        self.mirror_portal_attempts = 0
        self.mirror_auto_attempts = 0
        self.mirror_navigation_since = None
        self.mirror_multiplier_changes = 0
        self.mirror_multiplier_opens = 0
        self.mirror_multiplier_confirms = 0

    @staticmethod
    def stop(reason, status='needs_configuration'):
        return {'status': status, 'reason': reason}

    def click(self, kind, point, **extra):
        return {'status': 'action', 'type': kind, 'point': point, **extra}

    def stable(self, signature):
        if self.last_ready == signature:
            self.ready_observations += 1
        else:
            self.last_ready, self.ready_observations = signature, 1
        return self.ready_observations >= 2

    def cleanup(self, kind, point):
        event = (len(self.ledger.data['events']), kind)
        if self.cleanup_event != event:
            self.cleanup_event, self.cleanup_clicks = event, 0
        if self.cleanup_clicks >= 3:
            return self.stop('Result did not close after three attempts', 'needs_review')
        return self.click(kind, point, cleanup=True)

    def after_action(self, action):
        if action.get('cleanup'):
            self.cleanup_clicks += 1
        if action.get('adjustment'):
            self.adjustments += 1
        if action['type'] == 'select_hunting_category':
            self.selected_hunt_category = action['category']
            self.selected_hunt_stage = None
        if action['type'] == 'select_hunting_stage':
            self.selected_hunt_stage = action['target']
        if action['type'] == 'set_hunting_difficulty':
            self.difficulty_changes += 1
        if action['type'] == 'open_goddess_collection':
            self.goddess_collection_attempts += 1
            self.goddess_collection_transition_since = time.monotonic()
        if action['type'] == 'enter_goddess_square':
            self.goddess_scene_attempts += 1
            self.goddess_scene_transition_since = time.monotonic()
        mirror_navigation={'open_mirror_collection':'mirror_collection_attempts',
                           'enter_mirror_card':'mirror_card_attempts',
                           'enter_mirror_arena':'mirror_portal_attempts',
                           'open_mirror_auto':'mirror_auto_attempts',
                           'open_mirror_multiplier':'mirror_multiplier_opens',
                           'confirm_mirror_multiplier':'mirror_multiplier_confirms'}
        if action['type'] in mirror_navigation:
            field=mirror_navigation[action['type']]
            setattr(self,field,getattr(self,field)+1)
            self.mirror_navigation_since=time.monotonic()
        if action['type'].startswith('adjust_mirror_multiplier_'):
            self.mirror_multiplier_changes+=1

    def step(self, observation):
        state = observation['state']
        if state == 'unknown':
            self.last_ready, self.ready_observations = None, 0
        pending = self.ledger.data['pending']
        if pending:
            if pending['module'] != self.module:
                return self.stop('Another module has an unresolved submitted action', 'needs_review')
            match = False
            if self.module == 'hunting' and state == 'hunt_reward':
                count = observation.get('completed_runs')
                if count is not None and count != pending['quantity']:
                    return self.stop('Hunting result quantity differs from the submission', 'needs_review')
                if count is None:
                    before, after = pending.get('free_before'), observation.get('free_remaining')
                    if before is None or after is None or before-after != pending['cost']:
                        return self.stop('Hunt result lacks a verified quantity or matching resource deduction', 'needs_review')
                match = True
            elif self.module == 'mirror' and state == 'mirror_result':
                if observation.get('completed_battles') != pending['quantity']:
                    return self.stop('Mirror result count is unknown or differs from the submission', 'needs_review')
                match = True
            elif self.module == 'goddess':
                if pending['task'] == 'check_in':
                    match = state in ('goddess_check_in_result', 'goddess_loot_result') or (
                        state == 'goddess_options' and any(o['action'] == 'daily_reward' for o in observation['offers']))
                else:
                    match = state in ('goddess_reward_result', 'goddess_loot_result')
            if match:
                self.ledger.settle(observation)
                if self.module=='mirror':
                    self.mirror_auto_attempts,self.mirror_portal_attempts=0,0
                self.last_ready, self.ready_observations = None, 0
                return {'status': 'wait', 'reason': 'Submitted action result reconciled'}
            return {'status': 'wait', 'reason': 'Pending submission; observe its result without resubmitting'}
        if self.module == 'hunting':
            return self.hunting(observation)
        if self.module == 'goddess':
            return self.goddess(observation)
        if self.module == 'mirror':
            return self.mirror(observation)
        raise ValueError('Unsupported daily module')

    def hunting_plan(self):
        settings, progress = self.config['hunting'], self.ledger.data['hunting']
        if not progress['daily_done']:
            if settings['daily_task_status'] == 'done':
                progress['daily_done'] = True
                self.ledger.save()
            elif settings['daily_task_status'] == 'unknown':
                return None, self.stop('Confirm whether today\'s fixed hunting daily is already done')
            else:
                target = {**settings['daily_target']}
                target['stage'] = target.get('stage') or '野猪洞穴'
                return {'id': 'daily', 'runs': 1, **target}, None
        for plan in settings['extra_plans']:
            remaining = plan['runs'] - progress['runs'].get(plan['id'], 0)
            if remaining > 0:
                return {**plan, 'runs': remaining}, None
        return None, {'status': 'finished', 'reason': 'Configured hunting plans finished'}

    def hunting(self, obs):
        settings, progress = self.config['hunting'], self.ledger.data['hunting']
        if not settings['enabled']:
            return self.stop('Hunting disabled', 'skipped')
        if obs['state'] == 'hunt_reward':
            if (self.ledger.data.get('last_settled') or {}).get('module') == 'hunting':
                return self.cleanup('dismiss_hunt_reward', obs['dismiss_point'])
            return self.stop('Unowned hunting reward requires review', 'needs_review')
        if obs.get('daily_task_done'):
            progress['daily_done'] = True
            self.ledger.save()
        plan, end = self.hunting_plan()
        if end:
            if end['status'] == 'finished':
                if obs['state'] == 'hunt_dialog':
                    return self.cleanup('close_finished_hunt_dialog', obs['points']['cancel'])
                if obs['state'] == 'hunt_map':
                    return self.cleanup('return_from_hunting', obs['back_point'])
                if obs['state'] != 'main_menu':
                    return {'status': 'wait', 'reason': 'Waiting for main menu after hunting'}
            return end
        if self.active_hunt_plan != plan['id']:
            self.active_hunt_plan = plan['id']
            self.difficulty_changes = 0
            self.last_ready, self.ready_observations = None, 0
        if plan.get('target_mode') == 'lowest_stock':
            resolved = progress.setdefault('resolved_targets', {})
            if plan['id'] not in resolved:
                if obs['state'] == 'hunt_dialog':
                    return self.cleanup('close_hunt_for_target_change', obs['points']['cancel'])
                if obs['state'] != 'hunt_map':
                    return {'status': 'wait', 'reason': 'Open the holy-stone map to read its five inventories'}
                inventory = obs.get('stone_inventory')
                if not inventory or set(inventory) != set(STONE_STAGES):
                    point = obs.get('category_points', {}).get('圣石洞穴')
                    if point and not obs.get('stone_map'):
                        return self.click('select_hunting_category', point, category='圣石洞穴')
                    return self.stop('All five holy-stone inventories must be read before choosing the least', 'needs_review')
                if any(type(value) is not int or value < 0 for value in inventory.values()):
                    return self.stop('Holy-stone inventory is uncertain', 'needs_review')
                if not self.stable(('stone_inventory', plan['id'], tuple(inventory[s] for s in STONE_STAGES))):
                    return {'status': 'wait', 'reason': 'Verify all five stone inventories on a second observation'}
                stage = min(STONE_STAGES, key=lambda name: inventory[name])
                resolved[plan['id']] = {'stage': stage, 'inventory': inventory}
                progress['plan_targets'][plan['id']] = hunt_target_key(plan)
                self.ledger.save()
            plan = {**plan, 'stage': resolved[plan['id']]['stage']}
        if not plan.get('stage'):
            return self.stop('Choose a hunting stage; the recording stage is not a user preference')
        state = obs['state']
        if state == 'main_menu' and obs.get('hunt_entry_point'):
            return self.click('open_hunting', obs['hunt_entry_point'])
        if state == 'hunt_map':
            selected = (plan['category'], plan['stage'])
            if ((obs.get('selected_text') and plan['stage'] in obs['selected_text'])
                    or self.selected_hunt_stage == selected):
                if obs.get('difficulty') != plan['difficulty']:
                    if obs.get('difficulty') is None or not obs.get('difficulty_points'):
                        return self.stop('Hunting difficulty could not be read', 'needs_review')
                    if self.difficulty_changes >= 6:
                        return self.stop('Configured hunting difficulty was not reached', 'needs_review')
                    # A single step, then OCR the new value; never infer success.
                    if obs['difficulty'] not in HUNT_DIFFICULTIES or plan['difficulty'] not in HUNT_DIFFICULTIES:
                        return self.stop('Unsupported hunting difficulty', 'needs_review')
                    direction = 'next' if HUNT_DIFFICULTIES.index(plan['difficulty']) > HUNT_DIFFICULTIES.index(obs['difficulty']) else 'previous'
                    point = obs['difficulty_points'][direction]
                    return self.click('set_hunting_difficulty', point)
                return self.click('open_quick_hunt', obs['open_quick_point'])
            nodes = [n for n in [*obs['nodes'], *obs.get('stone_stage_points', [])]
                     if compact(plan['stage']) in n['text']]
            if len(nodes) == 1:
                return self.click('select_hunting_stage', nodes[0]['point'], target=selected)
            if plan['category'] in obs['category_points']:
                if self.selected_hunt_category != plan['category']:
                    return self.click('select_hunting_category', obs['category_points'][plan['category']], category=plan['category'])
                # Opening this dialog spends nothing. Its larger stage/difficulty
                # label must match the plan before any hunt can be submitted.
                return self.click('inspect_selected_hunting_stage', obs['open_quick_point'])
            return self.stop('Configured hunting stage is not identified on this map', 'needs_review')
        if state != 'hunt_dialog':
            return {'status': 'wait', 'reason': 'Waiting for a supported hunting page'}
        if not obs.get('stage_text'):
            return self.stop('Quick hunt dialog stage/difficulty differs from the plan', 'needs_review')
        if plan['stage'] not in obs['stage_text'] or plan['difficulty'] not in obs['stage_text']:
            return self.cleanup('close_hunt_for_target_change', obs['points']['cancel'])
        free_only = hunt_free_only(settings, plan)
        if obs.get('free_only') != free_only:
            if obs.get('free_only') is None:
                return self.stop('Free resource toggle is uncertain', 'needs_review')
            return self.click('set_hunt_free_only', obs['points']['free_toggle'])
        if obs.get('count') is None:
            return self.stop('Hunt quantity could not be read', 'needs_review')
        if obs['count'] != plan['runs']:
            return self.adjust_count(obs, plan['runs'])
        resource, cost = obs.get('resource'), obs.get('cost')
        if resource not in settings['budgets'] or type(cost) is not int or cost <= 0:
            return self.stop('Resource type/cost could not be verified', 'needs_review')
        budget = settings['budgets'][resource]
        if budget is None:
            return self.stop(f'Set the {resource} budget before spending it')
        if progress['spent'][resource] + cost > budget:
            return self.stop('Hunting resource budget reached', 'budget_reached')
        if free_only:
            if obs.get('free_available') is None:
                return self.stop('Available free hunting resource could not be read', 'needs_review')
            if obs['free_available'] < cost:
                return self.stop('Not enough free hunting resource', 'resources_exhausted')
        if not self.stable(('hunt', plan['id'], plan['runs'], cost, resource, obs['free_only'])):
            return {'status': 'wait', 'reason': 'Verify the same quantity/cost on a second observation'}
        return self.click('submit_quick_hunt', obs['points']['submit'], consumes=True, module='hunting',
                          plan_id=plan['id'], quantity=plan['runs'], resource=resource, cost=cost,
                          free_only=obs['free_only'], free_before=obs.get('free_available'),
                          resolved_stage=plan['stage'])

    def adjust_count(self, obs, target):
        self.last_ready, self.ready_observations = None, 0
        if self.adjustments >= 300:
            return self.stop('Quantity control did not reach its configured value', 'needs_review')
        if obs['count'] > target:
            return self.click('set_quantity_min', obs['points']['min'], adjustment=True)
        return self.click('increase_quantity_once', obs['points']['plus'], adjustment=True)

    def goddess(self, obs):
        settings = self.config['goddess']
        done = self.ledger.data['goddess']['done']
        if not settings['enabled']:
            return self.stop('Goddess interactions disabled', 'skipped')
        if not settings['user_positioned_near_statue']:
            return self.stop('User must stand beside the goddess statue', 'precondition_missing')
        if self.checkpoint!='available' and self.checkpoint not in settings['actions']:
            return self.stop('Requested goddess action is disabled', 'skipped')
        # Both launcher checkpoints sweep the enabled, currently available
        # interactions. The daily reward may become available only after fights.
        wanted = settings['actions']
        if all(task in done for task in wanted):
            return {'status': 'finished', 'reason': 'Requested statue interactions verified'}
        if (obs['state']!='goddess_scene' or obs.get('interaction_available') is not False):
            if self.last_ready and self.last_ready[0]=='goddess_no_available':
                self.last_ready,self.ready_observations=None,0
        if obs['state'] == 'main_menu':
            if self.goddess_scene_attempts:
                if self.goddess_scene_transition_since is not None and time.monotonic()-self.goddess_scene_transition_since<getattr(self, 'network_wait_seconds', 0):
                    return {'status':'wait','reason':'Waiting for Fantasia Square to load'}
                return self.stop('Square entry returned to menu without a verified interaction', 'needs_review')
            if self.goddess_collection_transition_since is not None and time.monotonic()-self.goddess_collection_transition_since<getattr(self, 'network_wait_seconds', 0):
                return {'status':'wait','reason':'Waiting for the card collection to open'}
            if not obs.get('collection_entry_point'):
                return self.stop('Main-menu collection entry was not recognized', 'needs_review')
            if self.goddess_collection_attempts >= 3:
                return self.stop('Could not open the card collection after three attempts', 'needs_review')
            return self.click('open_goddess_collection', obs['collection_entry_point'])
        if obs['state']=='goddess_collection':
            if self.goddess_scene_attempts:
                if time.monotonic()-self.goddess_scene_transition_since<getattr(self, 'network_wait_seconds', 45):
                    return {'status':'wait','reason':'Waiting for Fantasia Square to load'}
                return self.stop('Fantasia Square did not load after its entry was clicked', 'needs_review')
            if not obs.get('square_entry_point'):
                return self.stop('Fantasia Square card is not visible in the collection', 'needs_review')
            return self.click('enter_goddess_square',obs['square_entry_point'])
        if obs['state'] != 'goddess_options':
            if obs['state']=='goddess_scene':
                if obs.get('interaction_available') is False:
                    if not self.stable(('goddess_no_available',tuple(done))):
                        return {'status':'wait','reason':'Verify no statue interaction on a second observation'}
                    return {'status':'finished','reason':'No currently available statue interaction',
                            'completion_scope':'currently_available',
                            'deferred_actions':[task for task in wanted if task not in done]}
                return {'status':'waiting_for_option','reason':'No verified interaction glyph; do not mark it done'}
            if self.goddess_scene_transition_since is not None and time.monotonic()-self.goddess_scene_transition_since<getattr(self, 'network_wait_seconds', 45):
                return {'status':'wait','reason':'Waiting for Fantasia Square to load'}
            if self.goddess_collection_transition_since is not None and not self.goddess_scene_attempts and time.monotonic()-self.goddess_collection_transition_since<getattr(self, 'network_wait_seconds', 8):
                return {'status':'wait','reason':'Waiting for the card collection to open'}
            return {'status': 'waiting_for_option', 'reason': 'No verified interaction glyph; do not mark it done'}
        choices = [o for o in obs['offers'] if o['action'] in wanted and o['action'] not in done]
        if not choices:
            return {'status': 'waiting_for_option', 'reason': 'Remaining reward may require other challenges first'}
        if len(choices) != 1 or len(done) >= 2:
            return self.stop('Ambiguous statue option or daily interaction limit reached', 'needs_review')
        task = choices[0]['action']
        if not self.stable(('goddess', task)):
            return {'status': 'wait', 'reason': 'Verify interaction glyph on a second observation'}
        return self.click('interact_statue', obs['interaction_point'], consumes=True,
                          module='goddess', task=task, quantity=1, cost=0)

    def mirror(self, obs):
        settings, progress = self.config['mirror'], self.ledger.data['mirror']
        state = obs['state']
        if not settings['enabled']:
            return self.stop('Mirror battles disabled', 'skipped')
        if state == 'mirror_result':
            if (self.ledger.data.get('last_settled') or {}).get('module') == 'mirror':
                return self.cleanup('close_mirror_result', obs['close_point'])
            return self.stop('Unowned mirror result requires review', 'needs_review')
        if state == 'mirror_result_footer':
            return self.cleanup('leave_mirror_result', obs['leave_point'])
        remaining = settings['max_battles'] - progress['completed']
        if remaining <= 0:
            if state in ('mirror_auto_dialog','mirror_multiplier_dialog'):
                return self.cleanup('close_finished_'+state,obs['points']['cancel'])
            if state=='mirror_ready':
                return self.cleanup('leave_mirror_preparation',[76,38])
            if state in ('mirror_lobby','main_menu'):
                return {'status': 'finished', 'reason': 'Configured mirror battle count reached'}
            return {'status':'wait','reason':'Waiting for mirror return screen'}
        if progress['spent'] >= settings['resource_budget']:
            return self.stop('Mirror resource budget reached', 'budget_reached')
        if state=='mirror_multiplier_dialog':
            value=obs.get('multiplier');target=settings['resource_multiplier']
            if type(value) is not int or not 1<=value<=40:
                return self.stop('Mirror multiplier value could not be read','needs_review')
            if value==target:
                if self.mirror_multiplier_confirms>=3:
                    return self.stop('Mirror multiplier confirmation did not close','needs_review')
                return self.click('confirm_mirror_multiplier',obs['points']['confirm'])
            if self.mirror_multiplier_changes>=20:
                return self.stop('Mirror multiplier did not reach its configured value','needs_review')
            control='min' if value>target else 'plus5' if target-value>=5 else 'plus'
            return self.click('adjust_mirror_multiplier_'+control,obs['points'][control],adjustment=True)
        elapsed=time.monotonic()-self.mirror_navigation_since if self.mirror_navigation_since is not None else None
        if state=='main_menu':
            if self.mirror_card_attempts:
                if elapsed is not None and elapsed<getattr(self, 'network_wait_seconds', 0):
                    return {'status':'wait','reason':'Waiting for mirror navigation'}
                return self.stop('Mirror entry returned to menu before preparation was verified','needs_review')
            if elapsed is not None and elapsed<getattr(self, 'network_wait_seconds', 5):
                return {'status':'wait','reason':'Waiting for mirror navigation'}
            if not obs.get('collection_entry_point') or self.mirror_collection_attempts>=3:
                return self.stop('Could not open the mirror cartridge collection','needs_review')
            return self.click('open_mirror_collection',obs['collection_entry_point'])
        if state=='goddess_collection':
            if self.mirror_card_attempts:
                if elapsed is not None and elapsed<getattr(self, 'network_wait_seconds', 45):
                    return {'status':'wait','reason':'Waiting for mirror navigation'}
                return self.stop('Mirror cartridge did not finish loading','needs_review')
            if not obs.get('mirror_entry_point'):
                return self.stop('Mirror Wars cartridge is not visible','needs_review')
            return self.click('enter_mirror_card',obs['mirror_entry_point'])
        if state=='mirror_lobby':
            if self.mirror_portal_attempts and elapsed is not None and elapsed<getattr(self, 'network_wait_seconds', 2):
                return {'status':'wait','reason':'Waiting for mirror navigation'}
            if self.mirror_portal_attempts>=3 or not obs.get('arena_entry_point'):
                return self.stop('Mirror red platform could not open the preparation screen','needs_review')
            return self.click('enter_mirror_arena',obs['arena_entry_point'])
        if state == 'mirror_ready':
            if self.mirror_auto_attempts>=3:
                return self.stop('Mirror automatic battle window did not open','needs_review')
            if self.mirror_auto_attempts and elapsed is not None and elapsed<getattr(self, 'network_wait_seconds', 1.5):
                return {'status':'wait','reason':'Waiting for mirror navigation'}
            return self.click('open_mirror_auto', obs['open_auto_point'])
        if state != 'mirror_auto_dialog':
            if elapsed is not None and elapsed<getattr(self, 'network_wait_seconds', 45):
                return {'status':'wait','reason':'Waiting for mirror navigation'}
            return {'status': 'waiting_for_battle_screen', 'reason': 'Open mirror battle preparation with the chosen team'}
        # Normalize the game's repeat count before editing its resource multiplier.
        # The start button displays the per-battle price even when count is larger.
        if obs.get('count') is None:
            return self.stop('Mirror battle count could not be read', 'needs_review')
        if obs['count'] != 1:
            return self.adjust_count(obs, 1)
        if obs.get('multiplier') != settings['resource_multiplier']:
            if obs.get('multiplier') is None:
                return self.stop('Mirror multiplier value could not be read','needs_review')
            if self.mirror_multiplier_opens>=3:
                return self.stop('Mirror multiplier did not reach its configured value','needs_review')
            return self.click('open_mirror_multiplier',obs['points']['multiplier_settings'])
        for field, control in (('free_only', 'free_toggle'), ('auto_skip', 'skip_toggle')):
            if obs.get(field) != settings[field]:
                if obs.get(field) is None:
                    return self.stop(f'Mirror {field} toggle is uncertain', 'needs_review')
                return self.click('set_mirror_' + field, obs['points'][control])
        cost = obs.get('cost')
        if type(cost) is not int or cost <= 0:
            return self.stop('Mirror start button resource cost could not be read', 'needs_review')
        if progress['spent'] + cost > settings['resource_budget']:
            return self.stop('Next mirror battle would exceed the resource budget', 'budget_reached')
        if settings['free_only']:
            if obs.get('free_available') is None:
                return self.stop('Available free mirror resource could not be read', 'needs_review')
            if obs['free_available'] < cost:
                return self.stop('Not enough free mirror resource', 'resources_exhausted')
        if not self.stable(('mirror', 1, cost, obs['multiplier'], obs['free_only'], obs['auto_skip'])):
            return {'status': 'wait', 'reason': 'Verify mirror cost/settings on a second observation'}
        return self.click('submit_mirror_battle', obs['points']['submit'], consumes=True,
                          module='mirror', quantity=1, cost=cost)
