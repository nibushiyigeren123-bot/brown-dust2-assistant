"""复核两种正常返回的已有运行回执，供队列及用户进度共用；不修改账本。"""


def normal_unavailable_reason(report):
    """输入模块原报告；仅充分证据且已返主页、无active pending时返回正常跳过说明。"""
    if (report.get('execute') is not True or report.get('completed') is not True
            or report.get('returned_to_main_menu') is not True or report.get('error') or report.get('stopped')
            or report.get('pending') or (report.get('progress') or {}).get('pending')):
        return None
    unavailable=report.get('unavailable')
    observations=[item.get('observation',{}) for item in report.get('observations',[]) if isinstance(item,dict)]
    # [镜中结束] 单独校验策略、实际账本、两帧余额及本轮已提交操作的结算。
    if unavailable == 'mirror_resource_limit' and report.get('module') == 'mirror':
        evidence = report.get('mirror_resource_end') or {}
        policy, progress = report.get('mirror_policy'), (report.get('progress') or {})
        current = progress.get('mirror') or {}
        if (not isinstance(policy, dict) or evidence.get('policy') != policy
                or policy.get('enabled') is not True or type(policy.get('free_only')) is not bool
                or report.get('module_completed') is not False or report.get('skipped') is not True): return None
        if (type(policy.get('auto_skip')) is not bool or not isinstance(evidence.get('policy'), dict)
                or set(evidence['policy']) != set(policy)
                or any(type(evidence['policy'][key]) is not type(value) for key, value in policy.items())): return None
        if any(type((evidence.get('progress') or {}).get(key)) is not int for key in ('completed', 'spent')): return None
        if any(type(policy.get(key)) is not int or policy[key] < minimum for key, minimum in
               (('max_battles', 1), ('resource_budget', 0), ('resource_multiplier', 1))): return None
        if any(type(current.get(key)) is not int or current[key] < 0 for key in ('completed', 'spent')): return None
        if (evidence.get('progress') != {key: current[key] for key in ('completed', 'spent')}
                or current['completed'] >= policy['max_battles']): return None
        # 本轮每次消费必须有原输入回执与匹配镜中结果；不能用结束证据抹掉未核销消费。
        events = progress.get('events') or []
        for submitted in (a for a in report.get('actions', []) if a.get('consumes')):
            if submitted.get('module') != 'mirror' or not any(
                    (event.get('action') or {}).get('input_receipt')
                    and (event.get('result') or {}).get('state') == 'mirror_result'
                    and (event.get('result') or {}).get('completed_battles') == submitted.get('quantity')
                    and all((event.get('action') or {}).get(key) == submitted.get(key)
                            for key in ('module', 'quantity', 'cost', 'point')) for event in events): return None
        reason, pair = evidence.get('reason'), evidence.get('confirmed_balance')
        if reason == 'budget_reached':
            if pair != [] or current['spent'] < policy['resource_budget']: return None
            return '镜中之战已达到本次配置资源预算，已返回主菜单；保留实际结算场次，未补记未完成战斗。'
        if not isinstance(pair, list) or len(pair) != 2: return None
        if any(not isinstance(item, dict) or item.get('state') != 'mirror_auto_dialog' for item in pair): return None
        # [严格类型] 每个关键值按类型和值共同匹配，False/0.0不充当整数0。
        def same_sample(left, right):
            return set(left) == set(right) and all(type(left[key]) is type(right[key]) and left[key] == right[key] for key in left)
        if not same_sample(pair[0], pair[1]): return None
        # [来源核对] 两帧须来自本轮原观察或点击前复核，不能只相信结束对象自填。
        seen = []
        for record in report.get('observations', []):
            if not isinstance(record, dict): return None
            seen.append(record.get('observation') or {})
            if record.get('preclick'): seen.append(record['preclick'].get('observation') or {})
        keys = ('state', 'free_available', 'cost', 'multiplier', 'count', 'free_only', 'auto_skip')
        if len(seen) < 2 or any(not same_sample({key: item.get(key) for key in keys}, expected)
                                for item, expected in zip(seen[-2:], pair)): return None
        sample = pair[0]
        free, cost, multiplier = (sample.get(key) for key in ('free_available', 'cost', 'multiplier'))
        if reason in ('free_exhausted', 'free_insufficient'):
            if policy['free_only'] is not True or type(free) is not int or free < 0: return None
            if reason == 'free_exhausted':
                if free != 0: return None
                return '镜中之战免费体力已连续两帧确认为0，已返回主菜单；未使用额外库存，保留实际结算场次。'
            if (type(cost) is not int or cost <= 0 or not 0 < free < cost
                    or type(multiplier) is not int or multiplier != policy['resource_multiplier']): return None
            return '镜中之战免费体力不足当前单场费用，已连续两帧核对并返回主菜单；未使用额外库存，保留实际结算场次。'
        if reason == 'next_exceeds_budget':
            if (type(cost) is not int or cost <= 0 or type(multiplier) is not int
                    or multiplier != policy['resource_multiplier']
                    or type(sample.get('count')) is not int or sample['count'] != 1
                    or any(sample.get(key) is not policy.get(key) for key in ('free_only', 'auto_skip'))
                    or not current['spent'] < policy['resource_budget'] < current['spent'] + cost): return None
            return '镜中之战下一场将超过本次配置预算，已返回主菜单；保留实际结算场次，未提交超预算战斗。'
        return None
    if unavailable=='free_resources_exhausted' and report.get('module')=='hunting':
        survey=report.get('resource_survey') or {}
        resources=survey.get('resources') or {}
        if survey.get('completed') is not True or survey.get('consumes') is not False:
            return None
        # 可存在本次已经核销的合法狩猎，逐项与原events/输入回执相合；任何未决消费仍拒绝。
        events=(report.get('progress') or {}).get('events') or []
        for submitted in (a for a in report.get('actions',[]) if a.get('consumes')):
            if submitted.get('module')!='hunting':return None
            matched=False
            for event in events:
                old=event.get('action',{});receipt=event.get('result',{})
                if (old.get('input_receipt') and receipt.get('state')=='hunt_reward'
                        and all(old.get(k)==submitted.get(k) for k in ('module','plan_id','quantity','resource','cost','point'))):
                    matched=True;break
            if not matched:return None
        plans=report.get('free_exhaustion') or []
        if not plans or {p.get('plan_id') for p in plans}!=set(report.get('deferred_actions') or []):return None
        for plan in plans:
            name=plan.get('resource')
            if plan.get('free_only') is not True or name not in ('rice','torch'):return None
            balance=resources.get(name,{})
            if type(balance.get('free_available')) is not int or balance['free_available']!=0:
                return None
            pair=balance.get('confirmed_balance') or []
            if len(pair)!=2 or pair[0]!=pair[1]:return None
            if any(o.get('state')!='hunt_map' or o.get('resource')!=name
                   or type(o.get('free_available')) is not int or o['free_available']!=0
                   or type(o.get('free_capacity')) is not int or o['free_capacity']<=0 for o in pair):return None
        # 另一池也必须独立读清，允许有余额却没有用户已配置的对应消费计划。
        if any(type(resources.get(name,{}).get('free_available')) is not int for name in ('rice','torch')):return None
        both_zero=all(resources[name]['free_available']==0 for name in ('rice','torch'))
        return ('免费米饭与火把已分别连续两帧确认为0，已返回主菜单；未补记狩猎或每日任务完成。' if both_zero
                else '当前已配置免费计划的资源已耗尽，另一类资源已独立核对；已返回主菜单，未创建额外消费或补记未完成战斗。')
    # [无输入检查] 正确场景两帧及5秒证据仅证明未找到可用互动。
    if unavailable=='no_available_interaction' and report.get('module')=='goddess':
        events=(report.get('progress') or {}).get('events') or []
        result=(events[-1].get('result') or {}) if events else {}
        if (result.get('state')!='goddess_no_available_interaction'
                or type(result.get('stable_absence_seconds')) not in (int,float)
                or result['stable_absence_seconds']<5 or result.get('absence_frames',0)<2):return None
        if any(result.get(key,{}).get('state')!='goddess_scene' or result[key].get('interaction_control_state')!='absent'
               for key in ('first_absence','last_absence')):return None
        return '未找到可用互动；已在确认广场内连续5秒核对并返回主菜单，未核实的奖励仍未领取。'
    if unavailable=='no_hand_after_check_in' and report.get('module')=='goddess':
        events=(report.get('progress') or {}).get('events') or []
        if not events:
            return None
        action,result=events[-1].get('action',{}),events[-1].get('result',{})
        wait_evidence=result.get('no_hand_wait') or result
        confirmed=wait_evidence.get('scope')=='current_confirmed_check_in_no_hand'
        if confirmed and (result.get('state') not in ('goddess_check_in_result','goddess_loot_result','goddess_options')
                or 'check_in' not in (report.get('progress') or {}).get('goddess',{}).get('done',[])
                or ((report.get('progress') or {}).get('last_settled') or {}).get('task')!='check_in'):return None
        if (action.get('module')!='goddess' or action.get('task')!='check_in'
                or action.get('cost')!=0 or action.get('quantity')!=1 or not action.get('input_receipt')
                or wait_evidence.get('scope') not in ('current_zero_cost_attempt_only_unconfirmed','current_confirmed_check_in_no_hand')
                or type(wait_evidence.get('stable_absence_seconds')) not in (int,float)
                or wait_evidence['stable_absence_seconds']<5):
            return None
        clicks=[a for a in report.get('actions',[]) if a.get('consumes')]
        # [合并流程] 允许同轮先拍手后叹号；每次消费均须与真实结算事件相合，最后一笔仍是本次签到。
        if not clicks or any(clicks[-1].get(key)!=action.get(key) for key in ('module','task','cost','quantity','point')):
            return None
        for click in clicks[:-1]:
            if click.get('module')!='goddess' or not any(
                    event.get('action',{}).get('input_receipt') and event.get('result',{}).get('state') in ('goddess_reward_result','goddess_loot_result','goddess_check_in_result','goddess_options')
                    and all(event.get('action',{}).get(key)==click.get(key) for key in ('module','task','cost','quantity','point')) for event in events):
                return None
        # F正例存于实际提交意图；5秒首尾存于已结束事件，不依赖被裁掉的滚动observations。
        before=action.get('pre_observation') or {}
        if before.get('state')!='goddess_options' or before.get('task')!='check_in' or before.get('glyph')!='interaction_exclamation':return None
        if before.get('interaction_point')!=action.get('point'):return None
        if type(wait_evidence.get('absence_frames')) is not int or wait_evidence['absence_frames']<2:return None
        # [无手证据] 本次点叹号后，控件消失或仍为已核对叹号均表示尚未出现拍手；未知/遮挡不算。
        def verified_no_hand(item):
            return ((item.get('state')=='goddess_scene' and item.get('interaction_control_state')=='absent')
                    or (item.get('state')=='goddess_options' and item.get('interaction_control_state')=='visible_exclamation'
                        and item.get('offers') and all(offer.get('action')=='check_in' for offer in item['offers'])))
        if any(not verified_no_hand(wait_evidence.get(key) or {}) for key in ('first_absence','last_absence')):return None
        return ('签到已由原回执核实，连续5秒未观察到拍手，已返回主菜单；日常奖励仍未领取。' if confirmed else
                '已点击叹号，连续5秒未观察到拍手，当前零费用互动尝试结束并返回主菜单；未核实的签到和日常奖励仍未完成。')
    return None
