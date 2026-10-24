"""把当前有效步骤、55张共享全页图及局部UI模板编为可核验的CPU参考缓存；不训练、不操作游戏。"""
import argparse
import hashlib
import json
from pathlib import Path
import time
import types
import sys

import cv2
import numpy as np

from project_paths import ROOT

PREPROCESS = 'step_v1:GameVision.image_vector;global=private_mask;local=crop_before_embedding'

# [定义] 每一项对应module_catalog中的实际步骤；这些是识图目标，不是新的动作计划。
STAGES = {
 'navigation': [['main_menu','signin_event','signin_pass','reward_overlay'],['page_with_back','page_with_circle_back','scene_with_home','pass_tasks','pass_levels'],['main_menu'],['main_menu']],
 'management': [['main_menu'],['management_panel'],['reward','management_panel'],['main_menu']],
 'free_draw': [['gacha_page|costume'],['gacha_page|equipment'],['free_confirmation','draw_animation','draw_result'],['main_menu']],
 'guild': [['main_menu','guild_page','guild_check_in_receipt'],['main_menu','guild_page'],['guild_page','guild_check_in_receipt'],['main_menu']],
 'hunting': [['hunt_map','hunt_reward'],['hunt_map'],['hunt_map','hunt_dialog'],['hunt_dialog','hunt_reward'],['main_menu']],
 'goddess_check_in': [['main_menu','goddess_collection'],['goddess_scene','goddess_options'],['goddess_options','goddess_check_in_result','goddess_reward_result','goddess_loot_result'],['main_menu']],
 'mirror': [['main_menu'],['goddess_collection','mirror_lobby','mirror_ready'],['mirror_auto_dialog','mirror_multiplier_dialog','mirror_settlement'],['mirror_result','mirror_result_footer','mirror_settlement'],['main_menu']],
 'equipment': [['equipment_bag','equipment_grid'],['equipment_detail','equipment_refine'],['equipment_filter','equipment_grid'],['equipment_detail','equipment_decompose_confirmation','equipment_decompose_result'],['main_menu']],
 'event': [['event_hub','event_preparation'],['event_preparation|normal','event_preparation|challenge'],['event_quick_dialog','event_auto_dialog'],['event_auto_running','event_victory','event_reward','event_defeat'],['main_menu']],
 'goddess_daily_reward': [['main_menu','goddess_collection','goddess_scene'],['goddess_options'],['goddess_reward_result','goddess_loot_result'],['main_menu']],
 'rewards': [['pass_tasks|special'],['pass_levels|special'],['pass_tasks|season'],['pass_levels|season'],['main_menu']],
 'character_pass': [['pass_tasks|character','pass_levels|character'],['pass_tasks|character','pass_levels|character'],['reward_overlay','pass_tasks|character','pass_levels|character'],['main_menu']],
 'task_rewards': [['daily_tasks'],['weekly_tasks'],['weekly_tasks'],['daily_tasks','weekly_tasks','main_menu']],
}
FOLDERS = {
 'navigation':['m00','navigation','return_navigation','ui_foreground'], 'management':['management','return_navigation'],
 'free_draw':['free_draw'], 'guild':['phase08'], 'hunting':['daily_pending','hunting_options','hunting_resources'],
 'goddess_check_in':['goddess_entry','daily_pending'], 'goddess_daily_reward':['goddess_entry','daily_pending'],
 'mirror':['mirror_entry','daily_pending','goddess_entry'], 'equipment':['equipment'],
 'event':['event_prepare','event_battle','phase08'], 'rewards':['pass_rewards'], 'character_pass':['pass_rewards'], 'task_rewards':['task_rewards'],
}
POLICY = {
 'navigation':'确认前景控件；主菜单须连续观察；模型不能提供退出坐标。',
 'management':'经营标题与按钮亮暗必须成立；有收益才领，灰色跳过，奖励与返回各自核对。',
 'free_draw':'服装/装备独立识别；免费字样和确认按钮必须同在；付费/未知确认绝不通行。',
 'guild':'公会标题/公告/聊天独立确认；只有成功字形加邮箱文字才是收据；无收据访问不记issued。',
 'hunting':'按v85读取米饭与火把；未知不是零；余额、免费开关、关卡、次数、成本仍由原门禁核对。',
 'character_pass':'固定角色通行证后缀与PASS页面结构独立核对；前缀仅metadata。免费领取需按钮及结果回执，缺全页样本明确缺失。',
 'goddess_check_in':'v89合并：右下F叹号是check_in，拍手是daily_reward；本次有输入回执且叹号消失无手五秒只可记未证实尝试，不能写done。',
 'goddess_daily_reward':'v85：只认右下F拍手为daily_reward；叹号属check_in；未开放/无手不冒充领到。',
 'mirror':'原阵容不改；倍率/次数/免费开关独立核对；赛季末只能用原结算判据跳过。',
 'equipment':'普通R列表→独立品质/未锁未穿戴详情→剪刀→确认1件→结果/返回图像链；R优先，旧批选仅作历史证据恢复，全页相似不能授权分解。',
 'event':'活动身份、普通/挑战、AP、数量和免费属性由原逻辑核对；模型不能给出消费许可。',
 'rewards':'特别/赛季身份和任务/等级页签独立确认；只走一键领，排除购买区域。',
 'task_rewards':'每日/每周标题与区间奖励文字共同确认；按钮亮暗和任务完成进度独立复核。',
}

# [定义] 各页面阶段分别看哪些原始UI裁片与文字区域；同态异身份保留单独规则组。
STATE_RULES={
 'signin_event':(['m00:signin_event'],[],['opening.suggested_point_normalized','next_action']),
 'signin_pass':(['m00:signin_pass'],[],['opening.suggested_point_normalized','next_action']),
 'reward_overlay':(['step_vision:reward_'],[],['visual_scores','next_action']),
 'page_with_back':(['return_navigation:white_back'],[],['return_button','next_action']),
 'page_with_circle_back':(['return_navigation:circle_back','return_navigation:library_back'],[],['return_button','next_action']),
 'scene_with_home':(['step_vision:scene_home','navigation:square_home_'],[],['home_button','next_action']),
 'main_menu':(['ui_foreground:','free_draw:home_entry','phase08:guild_entry'],[],['state','entry_point']),
 'management_panel':(['return_navigation:management_'],[],['panel.state','panel.brightness_ratio']),
 'reward':(['management:','return_navigation:management_'],[],['state']),
 'gacha_page':(['free_draw:costume_','free_draw:equipment_','free_draw:paid_label','free_draw:batch_free','free_draw:back'],[],['category','free_available','free_point']),
 'free_confirmation':(['free_draw:confirm_','free_draw:cancel_','free_draw:free_word'],[],['free_word','yellow_free_word','confirm_point']),
 'draw_animation':(['free_draw:skip_','free_draw:animation_loop'],[],['skip_control','animation_loop']),
 'draw_result':(['free_draw:result_badge','free_draw:equipment_result_badge','free_draw:back'],[],['back_point','pale_fraction']),
 'guild_page':(['phase08:guild_title'],[[130,235,300,290],[500,60,600,115]],['check_in_status','points.back']),
 'guild_check_in_receipt':(['phase08:guild_title','phase08:guild_success'],[[130,235,300,290],[500,60,600,115],[550,475,730,520]],['success_anchor','reward_destination']),
 'hunt_map':(['daily_pending:hunt_map','hunting_resources:','hunting_options:'],[[960,0,1275,85],[140,0,370,85],[155,620,330,700],[250,80,1060,600]],['resource','free_available','owned_extra','difficulty','daily_task_done']),
 'hunt_dialog':(['daily_pending:hunt_title','return_navigation:hunt_'],[[390,150,875,560],[960,0,1275,85]],['stage_text','resource','count','cost','free_only']),
 'hunt_reward':(['daily_pending:hunt_reward'],[[530,200,755,300],[960,0,1275,85]],['completed_runs','free_remaining']),
 'goddess_collection':(['goddess_entry:library_title','goddess_entry:square_logo','mirror_entry:card_logo'],[],['square_entry_point','mirror_entry_point']),
 'goddess_scene':(['goddess_entry:square_team','daily_pending:goddess_scene_'],[[620,65,1275,170]],['interaction_available','evidence']),
 'goddess_options':(['daily_pending:goddess_hand','daily_pending:goddess_reward_ready','daily_pending:goddess_f_key','daily_pending:goddess_scene_'],[[560,65,1040,270],[1010,65,1275,145]],['offers[].action','interaction_point','interaction_evidence.button_verified','interaction_evidence.control_state']),
 'goddess_check_in_result':(['daily_pending:goddess_scene_'],[[700,0,1100,85]],['state']),
 'goddess_reward_result':(['daily_pending:goddess_reward_loot'],[],['evidence']),
 'goddess_loot_result':(['goddess_entry:square_team'],[[30,270,425,550],[1050,55,1275,140]],['loot']),
 'mirror_lobby':(['mirror_entry:lobby_'],[],['arena_entry_point']),
 'mirror_ready':(['daily_pending:mirror_auto_entry','daily_pending:mirror_battle'],[],['open_auto_point']),
 'mirror_auto_dialog':(['daily_pending:mirror_auto_title','mirror_entry:auto_','mirror_entry:price_'],[[370,185,910,560],[960,0,1275,85]],['count','multiplier','cost','free_only','auto_skip']),
 'mirror_multiplier_dialog':(['mirror_entry:multiplier_title','mirror_entry:editor_'],[[370,180,910,600]],['multiplier']),
 'mirror_result':(['daily_pending:mirror_result_title'],[[530,195,750,405]],['completed_battles','close_point']),
 'mirror_result_footer':(['daily_pending:mirror_result_leave'],[[1065,630,1210,700]],['leave_point']),
 'mirror_settlement':(['daily_pending:mirror_auto_entry','daily_pending:mirror_battle'],[[150,30,1120,420]],['settlement_evidence']),
 'equipment_bag':(['equipment:bag_consumables_'],[],['state']),
 'equipment_grid':(['equipment:grid_','equipment:quality_','equipment:filter_button'],[[1080,0,1275,90]],['inventory','empty_filtered_grid']),
 'equipment_detail':(['equipment:detail_','equipment:quality_'],[[500,80,1000,620]],['quality','unlocked','not_equipped','score20']),
 'equipment_refine':(['equipment:refine_'],[[350,80,1000,650]],['score','before','cost']),
 'equipment_filter':(['equipment:filter_'],[],['selected_qualities','worn_hidden','default_visible_options','saved_toast']),
 'equipment_decompose_selection':(['equipment:selected_','equipment:decompose_select_'],[],['matches_reviewed_one_item_selection']),
 'equipment_decompose_confirmation':(['equipment:decompose_confirm_'],[],['matches_reviewed_one_item_confirmation']),
 'equipment_decompose_result':(['equipment:decompose_result_'],[],['matches_reviewed_55_5_result']),
 'event_hub':(['event_prepare:hub_loyal_maid','event_prepare:home_loyal_maid'],[],['event_id']),
 'event_preparation':(['event_battle:auto_entry','phase08:unit_'],[[570,65,950,200],[960,0,1275,85],[650,635,950,700]],['event_id','mode','stage','unit_cost','free_ap']),
 'event_quick_dialog':(['return_navigation:event_quick_'],[[350,160,940,590],[960,0,1275,85]],['quantity','total_cost','free_only']),
 'event_auto_dialog':(['return_navigation:event_auto_','phase08:cost_'],[[350,160,940,590],[960,0,1275,85]],['quantity','total_cost','free_only']),
 'event_auto_running':(['phase08:victory'],[[900,65,1275,230]],['auto_progress']),
 'event_victory':(['phase08:victory'],[[900,65,1275,230]],['mode','stage','auto_progress']),
 'event_reward':(['daily_pending:hunt_reward'],[[400,595,900,700]],['state']),
 'event_defeat':(['return_navigation:circle_back'],[[500,70,1200,230]],['state']),
 'pass_tasks':(['pass_rewards:identity:','pass_rewards:tab:'],[[155,15,265,55],[450,190,1000,490]],['pass_id','tab','claimable','incomplete_visible']),
 'pass_levels':(['pass_rewards:identity:','pass_rewards:tab:'],[[155,15,265,55],[380,190,1000,490]],['pass_id','tab','claimable','current_level']),
 'daily_tasks':(['task_rewards:daily'],[[370,15,480,55],[450,85,950,650]],['mode','claimable','rows','incomplete_visible']),
 'weekly_tasks':(['task_rewards:weekly'],[[370,15,480,55],[450,85,950,650]],['mode','claimable','rows','incomplete_visible']),
}


def state_rule(state, local):
    """为实际页面建立精确局部组；无法定位的阶段明确缺判据，不借其他页面补覆盖。"""
    base=state.split('|')[0]
    patterns,rois,fields=STATE_RULES.get(base,([],[],[]))
    selected=[key for key,spec in local.items() if any((spec['folder']+':'+spec['name']).startswith(p) for p in patterns)]
    if '|' in state and base.startswith('pass_') and state.split('|')[1]!='character':
        identity=state.split('|')[1]
        selected=[key for key in selected if identity in local[key]['name']]
    if '|' in state and base=='gacha_page':
        category=state.split('|')[1]
        selected=[key for key in selected if not local[key]['name'].endswith('_title') or local[key]['name'].startswith(category)]
    controls={'main_menu':'opening.menu','signin_event':'opening.recognize','signin_pass':'opening.recognize',
              'reward_overlay':'reward_overlay.inspect','page_with_back':'returns.inspect',
              'page_with_circle_back':'returns.inspect','scene_with_home':'find_home'}
    validator=controls.get(base)
    return {'shared_group':state,'local_keys':selected,'ocr_rois':rois,'fields':fields,
            'existing_control_validator':validator,
            'validator':'原'+base+'识图分支与对应控制器条件',
            'missing_local_rule':not bool(selected or validator),'stage_status':'shared_observed_state_not_click_counter'}


def digest(data):
    """返回原字节SHA256，用于绑定缓存与资产身份。"""
    return hashlib.sha256(data).hexdigest()


def local_specs(root):
    """只收集现有UI裁片及其ROI，剥离私人来源路径；不自动重建任何原图。"""
    result = {}
    def walk(value, folder, prefix=''):
        """递归读取既有声明中的文件与坐标，只输出不含私人来源的UI元数据。"""
        if isinstance(value, dict):
            box = value.get('box',value.get('box_1280'))
            if value.get('file') and isinstance(box,list) and len(box)==4:
                path = folder / value['file']
                if path.is_file() and path.resolve().is_relative_to((root/'assets').resolve()):
                    key=path.relative_to(root).as_posix()
                    result[key]={'file':key,'box':box,'sha256':digest(path.read_bytes()),'name':value.get('id',prefix),
                                 'gray_threshold':value.get('gray_threshold'),'maximum_channel_spread':value.get('maximum_channel_spread'),
                                 'folder':folder.name,'kind':'raw_local_UI_not_global_mask'}
            else:
                for key, item in value.items():walk(item,folder,str(key))
        elif isinstance(value,list):
            for item in value:walk(item,folder,prefix)
    for folder in sorted({name for names in FOLDERS.values() for name in names}):
        for path in sorted((root/'assets'/folder).glob('*.json')):
            walk(json.loads(path.read_text(encoding='utf-8-sig')),path.parent)
    # [来源] 从已核hash的共享图裁出既有导航validator使用的局部UI，不重绘、不含账号原图。
    shared=json.loads((root/'assets/shared_references/index.json').read_text(encoding='utf-8'))['templates']
    derived=[('reward_title','reward_overlay',[585,209,695,239]),('reward_hint','reward_overlay',[579,651,697,669])]
    from common_navigation import PageDiagnosis
    scene=shared.get('scene_with_home')
    if scene:
        raw=(root/'assets/shared_references'/scene['file']).read_bytes()
        if digest(raw)!=scene['sha256']:raise ValueError('Shared scene changed')
        frame=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_COLOR)
        control=PageDiagnosis().find_home(frame)
        if control:
            x,y,w,h=control['client_box'];derived.append(('scene_home','scene_with_home',[round(x),round(y),round(x+w),round(y+h)]))
    for name,key,box in derived:
        spec=shared[key];raw=(root/'assets/shared_references'/spec['file']).read_bytes()
        if digest(raw)!=spec['sha256']:raise ValueError('Shared anchor source changed')
        frame=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_COLOR)
        a,b,c,d=box;path=root/'assets/step_vision'/('local_'+name+'.png')
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(cv2.imencode('.png',frame[b:d,a:c])[1].tobytes())
        relative=path.relative_to(root).as_posix()
        result[relative]={'file':relative,'box':box,'sha256':digest(path.read_bytes()),'name':name,'folder':'step_vision',
                          'kind':'raw_local_UI_not_global_mask','derived_from_shared':key,'source_reference_sha256':spec['sha256']}
    return result


def build_targets(root):
    """建立当前有效模块步骤的持久映射；完整画面缺失与局部图缺失分别列出，不补造标签。"""
    from module_catalog import MODULES
    shared=json.loads((root/'assets/shared_references/index.json').read_text(encoding='utf-8'))['templates']
    # [已确认补图] v85用户当前F叹号原图只在本地生成时读取；发布资产只含账号遮罩派生图和原hash。
    confirmed=root/'evidence/hunt_goddess_v85/fixtures/failed_goddess_original.png'
    if confirmed.is_file():
        raw=confirmed.read_bytes()
        expected='ebbf351acd3ea477431d764e52ef3b1c728bfa91d5cbd74a9f52ac26a906faba'
        if digest(raw)!=expected:raise ValueError('Approved v85 F screenshot changed')
        from frame_store import private_mask
        frame=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_COLOR)
        frame=private_mask(cv2.resize(frame,(1280,720),interpolation=cv2.INTER_AREA))
        path=root/'assets/step_vision/v85_confirmed_f.png'
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(cv2.imencode('.png',frame)[1].tobytes())
        shared['goddess_options|v85_confirmed_f']={'file':'../step_vision/v85_confirmed_f.png',
            'sha256':digest(path.read_bytes()),'state':'goddess_options','source_sha256':expected,
            'module':'goddess_check_in','supplemental':True,'label_scope':'v85 confirmed lower-right F exclamation=check_in'}
    # [保留] 发布工程可能不含私人原图，已核验并遮罩的F参考仍须按原hash保留，不能重算时丢失。
    retained=root/'assets/step_vision/v85_confirmed_f.png'
    if 'goddess_options|v85_confirmed_f' not in shared and retained.is_file():
        expected_asset='d45ab97e1c69e507ab486a934d7798d5ad8867b1ac1efc2713c0400e994c5661'
        if digest(retained.read_bytes())!=expected_asset:raise ValueError('Retained approved F reference changed')
        shared['goddess_options|v85_confirmed_f']={'file':'../step_vision/v85_confirmed_f.png','sha256':expected_asset,
            'state':'goddess_options','source_sha256':'ebbf351acd3ea477431d764e52ef3b1c728bfa91d5cbd74a9f52ac26a906faba',
            'module':'goddess_check_in','supplemental':True,'label_scope':'v85 confirmed lower-right F exclamation=check_in'}
    local=local_specs(root)
    targets=[]
    validators={'navigation':'common_navigation.PageDiagnosis.inspect / ensure_main_menu',
        'management':'m01_management.ManagementRecognizer.inspect / run_management',
        'free_draw':'m02_free_draw.FreeDrawRecognizer.inspect / FreeDrawController.step',
        'guild':'phase08_vision.Phase08Vision.inspect / guild_check_in.GuildController.step',
        'equipment':'equipment_vision.EquipmentVision.inspect / equipment_tasks.py原账本核验',
        'event':'event_battle_vision.EventBattleVision.inspect / event_battle_flow.EventBattleController.step',
        'rewards':'pass_reward_vision.PassRewardVision.inspect / pass_rewards.PassRun',
        'task_rewards':'task_reward_vision.TaskRewardVision.inspect / task_rewards.TaskRewardRun'}
    for module in MODULES:
        if not module.worker:continue
        stages=STAGES[module.id]
        if len(stages)!=len(module.steps):raise ValueError('Step contract changed: '+module.id)
        for index,(goal,states) in enumerate(zip(module.steps,stages),1):
            keys=[k for k in shared if any(k==s or ('|' not in s and k.split('|')[0]==s) for s in states)]
            groups={state:state_rule(state,local) for state in states}
            anchors=sorted({k for group in groups.values() for k in group['local_keys']})
            targets.append({'step_id':f'{module.id}.{index:02d}','module':module.id,'ordinal':index,
                'goal':goal,'states':states,'criteria':POLICY[module.id],
                'completion_condition':'原控制器/账本确认；识图不写完成状态', 'observed_groups':groups,
                'validator_entry':validators.get(module.id,'daily_vision.DailyVision.inspect / inspect_frame / daily_tasks.DailyController.step'),
                'reference_keys':keys,'local_keys':anchors,
                'missing_full_states':[s for s in states if not any(k==s or ('|' not in s and k.split('|')[0]==s) for k in keys)],
                'ocr_rois':[list(box) for box in sorted({tuple(box) for group in groups.values() for box in group['ocr_rois']})],
                'coverage':'references_and_local_rules_available' if keys and anchors else 'missing_reference_or_local_rule',
                'local_title_policy':'标题ROI仅在局部锚点匹配后从原始内存UI读取；全页向量仍遮罩账号区'})
    # [筛选] 只发布当前有效步骤真实消费的局部参考；背景/旧模板不能进入缓存身份或隐式要求补发。
    consumed={key for target in targets for key in target['local_keys']}
    local={key:spec for key,spec in local.items() if key in consumed}
    if consumed != set(local):raise ValueError('Consumed local reference missing')
    return {'schema_version':1,'preprocess':PREPROCESS,'enabled':True,'modules':[module.id for module in MODULES if module.worker],
            'targets':targets,'locals':local,'global_references':shared,
            'scope':'步骤参考与识图路由；不授予点击、余额、免费或完成权限'}


def fingerprint(root, targets):
    """把清单、预处理代码、模型和每张图的实际hash一起绑定；任何变化都使缓存失效。"""
    files=[p.relative_to(root).as_posix() for p in (root/'models/gamevision_bd2').iterdir() if p.is_file()]
    # [身份] PyInstaller会归档源码，运行时不依赖散装.py；签名只取已嵌入函数字节码和常量。
    # 排除co_filename/行号，使同Python版本的源码与冻结入口计算相同签名。
    from gamevision_runtime import GameVision
    from frame_store import private_mask
    from step_vision import StepVision
    def code_data(code):
        """提取稳定可嵌入签名；不包括源码路径、行号或对象地址。"""
        return {'bytes':code.co_code.hex(),'names':code.co_names,'vars':code.co_varnames,
                'constants':[code_data(c) if isinstance(c,types.CodeType) else repr(c) for c in code.co_consts]}
    signatures={name:digest(json.dumps(code_data(fn.__code__),sort_keys=True).encode()) for name,fn in
                [('visual_input',GameVision.image_vector),('private_mask',private_mask),('step_route',StepVision.route),
                 ('local_input',StepVision._local_evidence),('local_route',StepVision.local_route)]}
    files += ['assets/shared_references/'+v['file'] for v in targets['global_references'].values()]
    files += list(targets['locals'])
    identity={name:digest((root/name).read_bytes()) for name in sorted(set(files))}
    for spec in targets['global_references'].values():
        if identity['assets/shared_references/'+spec['file']]!=spec['sha256']:raise ValueError('Global reference changed')
    for key,spec in targets['locals'].items():
        if identity[key]!=spec['sha256']:raise ValueError('Local reference changed')
    if targets['preprocess']!=PREPROCESS:raise ValueError('Preprocessing schema changed')
    return digest(json.dumps({'targets':targets,'files':identity,'code':signatures,'python':sys.implementation.cache_tag},ensure_ascii=False,sort_keys=True).encode('utf-8'))


def prepare(root=ROOT):
    """真正逐图CPU推理并保存向量、跨状态候选及身份清单；不把自匹配当泛化验证。"""
    from gamevision_runtime import GameVision
    root=Path(root);folder=root/'assets/step_vision';folder.mkdir(parents=True,exist_ok=True)
    targets=build_targets(root);identity=fingerprint(root,targets)
    engine=GameVision(root/'models/gamevision_bd2',threads=2)
    vectors=[];keys=[];rows=[];started=time.perf_counter()
    for key,spec in targets['global_references'].items():
        frame=cv2.imdecode(np.frombuffer((root/'assets/shared_references'/spec['file']).read_bytes(),np.uint8),cv2.IMREAD_COLOR)
        vectors.append(engine.image_vector(frame));keys.append('global:'+key)
        rows.append({'key':key,'state':spec['state'],'kind':'global'})
    global_count=len(vectors)
    for key,spec in targets['locals'].items():
        patch=cv2.imdecode(np.frombuffer((root/key).read_bytes(),np.uint8),cv2.IMREAD_COLOR)
        vectors.append(engine.image_vector(patch));keys.append('local:'+key)
        rows.append({'key':key,'kind':'local'})
    matrix=np.stack(vectors).astype(np.float32)
    if not np.isfinite(matrix).all():raise ValueError('Nonfinite reference vectors')
    scores=matrix[:global_count]@matrix[:global_count].T
    for i,row in enumerate(rows[:global_count]):
        row['self_similarity']=float(scores[i,i])
        others=[int(j) for j in np.argsort(-scores[i]) if rows[int(j)]['state']!=row['state']][:3]
        row['nearest_other_states']=[{'key':rows[j]['key'],'state':rows[j]['state'],'score':float(scores[i,j])} for j in others]
    np.savez_compressed(folder/'vectors.npz',keys=np.asarray(keys),vectors=matrix)
    (folder/'targets.json').write_text(json.dumps(targets,ensure_ascii=False,indent=2),encoding='utf-8')
    cache={'schema_version':1,'fingerprint':identity,'cache_sha256':digest((folder/'vectors.npz').read_bytes()),
           'global_count':global_count,'shared_count':sum(not s.get('supplemental',False) for s in targets['global_references'].values()),
           'supplemental_count':sum(bool(s.get('supplemental')) for s in targets['global_references'].values()),'local_count':len(vectors)-global_count,
           'provider':engine.session('visual').get_providers(),'elapsed_seconds':time.perf_counter()-started,
           'rows':rows,'scope':'真实CPU预读与参考集内混淆；不是独立泛化测试','game_input':False}
    (folder/'cache.json').write_text(json.dumps(cache,ensure_ascii=False,indent=2),encoding='utf-8')
    del engine
    return cache


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=ROOT)
    result=prepare(parser.parse_args().root)
    print(json.dumps({k:v for k,v in result.items() if k!='rows'},ensure_ascii=False))
