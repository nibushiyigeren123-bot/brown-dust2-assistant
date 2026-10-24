"""真实CPU离线过图与包装契约验收；不发现游戏窗口，不运行worker，不提交任何动作。"""
import argparse
import ast
import shutil
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

import cv2
import numpy as np

from project_paths import ROOT
from prepare_step_vision import digest
from step_vision import StepVision, self_check, release, _contexts
import step_vision_hooks as hooks


def image(path):
    """只读保存图，不连接窗口；中文路径按原始字节解码。"""
    value=cv2.imdecode(np.frombuffer(Path(path).read_bytes(),np.uint8),cv2.IMREAD_COLOR)
    if value is None:raise ValueError('Unreadable offline image')
    return cv2.resize(value,(1280,720),interpolation=cv2.INTER_AREA)


def adapter(module, root, output):
    """返回真实识图对象及调用参数，不创建window或执行器；装备库存扫描限离线识图。"""
    if module=='navigation':
        from common_navigation import PageDiagnosis
        return PageDiagnosis(),lambda payload:()
    if module=='management':
        from m01_management import ManagementRecognizer
        return ManagementRecognizer(),lambda payload:()
    if module=='free_draw':
        from m02_free_draw import FreeDrawRecognizer
        return FreeDrawRecognizer(),lambda payload:()
    if module=='guild':
        from phase08_vision import Phase08Vision
        return Phase08Vision(),lambda payload:(payload,)
    if module in ('hunting','goddess_check_in','goddess_daily_reward','mirror'):
        from daily_vision import DailyVision
        config=json.loads((root/'config/daily_tasks.json').read_text(encoding='utf-8-sig'))
        return DailyVision(),lambda payload:(payload,config)
    if module=='equipment':
        from equipment_vision import EquipmentVision
        from daily_vision import OcrDocument
        return EquipmentVision(),lambda payload:(OcrDocument(payload),output)
    if module=='event':
        from event_battle_vision import EventBattleVision
        return EventBattleVision(),lambda payload:(payload,)
    if module=='rewards':
        from pass_reward_vision import PassRewardVision
        return PassRewardVision(),lambda payload:(payload,)
    from task_reward_vision import TaskRewardVision
    return TaskRewardVision(),lambda payload:(payload,)


def portable_and_error_checks(root, output, targets, check):
    """模拟构建器真实资产排除，并在新进程验证缓存和只写指定目录的异常出口。"""
    # [夹具] 从当前构建器提取原函数，只执行asset_ignore，不导入或执行构建入口。
    tree=ast.parse((root/'tools/build_portable.py').read_text(encoding='utf-8-sig'))
    functions=[node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef) and node.name=='asset_ignore']
    if len(functions)!=1:raise ValueError('Portable asset_ignore identity is not unique')
    namespace={'Path':Path,'PROJECT':root}
    exec(compile(ast.fix_missing_locations(ast.Module(body=functions,type_ignores=[])),'portable_asset_ignore','exec'),namespace)
    ignored=[]
    def actual_ignore(directory,names):
        """调用构建器实际排除规则并记录被省略资产，防止夹具与发布条件脱节。"""
        selected=namespace['asset_ignore'](directory,names)
        ignored.extend((Path(directory)/name).relative_to(root).as_posix() for name in selected if name in names)
        return selected
    fixture=output/'portable_asset_fixture';fixture.mkdir()
    shutil.copytree(root/'assets',fixture/'assets',ignore=actual_ignore)
    shutil.copytree(root/'models',fixture/'models')
    shutil.copytree(root/'config',fixture/'config')
    consumed={key for target in targets['targets'] for key in target['local_keys']}
    check('locals_exactly_consumed_by_steps',set(targets['locals'])==consumed)
    check('portable_omits_home_backgrounds',any(name.startswith('assets/m00/home_') for name in ignored)
          and not list((fixture/'assets/m00').glob('home_*')),ignored)
    check('all_cached_references_shipped',all((fixture/key).is_file() for key in targets['locals'])
          and all((fixture/'assets/shared_references'/spec['file']).is_file() for spec in targets['global_references'].values()))
    # [新进程] 构造时禁止任何ONNX推理；随后真实CPU查询，验证指纹、加载与释放。
    code="""import sys,json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
from step_vision import StepVision,self_check
import onnxruntime as ort
with patch.object(ort.InferenceSession,'run',side_effect=AssertionError('reference recomputation')) as call:
    ctx=StepVision(Path(sys.argv[2]),'navigation')
    loaded=len(ctx.keys);calls=call.call_count;ctx.close()
report=self_check(Path(sys.argv[2]),Path(sys.argv[3]))
print(json.dumps({'loaded':loaded,'constructor_run_calls':calls,'report':report}))
"""
    child=subprocess.run([sys.executable,'-B','-X','utf8','-c',code,str(root/'tools'),str(fixture),str(output/'portable_cpu_check')],
                         capture_output=True,text=True,encoding='utf-8',timeout=120)
    details=json.loads(child.stdout) if child.returncode==0 else {'error':child.stderr,'returncode':child.returncode}
    (output/'portable_cpu_child.json').write_text(json.dumps(details,ensure_ascii=False,indent=2),encoding='utf-8')
    check('portable_fresh_process_identity_without_reference_inference',child.returncode==0 and details['constructor_run_calls']==0
          and details['loaded']==len(targets['global_references'])+len(consumed),details)
    report=details.get('report',{})
    check('portable_actual_cpu_query_and_release',child.returncode==0 and report.get('providers')==['CPUExecutionProvider']
          and report.get('reference_inferences')==0 and report.get('query_inferences',0)>0
          and report.get('released') is True and report.get('cache_identity_verified') is True,report)

    # [异常回归] 完整运行assistant_app的__main__异常处理；拦截弹窗与越界mkdir/write_text。
    diagnostic_code="""import sys,json,runpy,types,builtins
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
import project_paths
project_paths.ROOT=Path(sys.argv[2])
flag=sys.argv[4];mode=sys.argv[5];output=Path(sys.argv[3]).resolve()
module='step_vision' if flag=='--step-vision-self-check' else 'verify_step_vision'
def failure(*args,**kwargs):raise RuntimeError('forced diagnostic runtime failure')
stub=types.ModuleType(module);stub.self_check=failure;stub.verify=failure
sys.modules[module]=stub
original_import=builtins.__import__
def guarded_import(name,*args,**kwargs):
    if mode=='import' and name==module:raise ImportError('forced diagnostic import failure')
    return original_import(name,*args,**kwargs)
def modal(*args,**kwargs):raise AssertionError('modal must never be called')
original_mkdir=Path.mkdir;original_write=Path.write_text;writes=[]
def mkdir(path,*args,**kwargs):
    if not path.resolve().is_relative_to(output):raise AssertionError('unexpected mkdir '+str(path))
    return original_mkdir(path,*args,**kwargs)
def write(path,*args,**kwargs):
    if not path.resolve().is_relative_to(output):raise AssertionError('unexpected write '+str(path))
    writes.append(str(path));return original_write(path,*args,**kwargs)
sys.argv=[str(Path(sys.argv[1])/'assistant_app.py'),flag,str(output)]
try:
    with patch('tkinter.messagebox.showerror',modal),patch.object(Path,'mkdir',mkdir),patch.object(Path,'write_text',write),patch('builtins.__import__',guarded_import):
        runpy.run_path(sys.argv[0],run_name='__main__')
except SystemExit as exit:
    print(json.dumps({'exit_code':exit.code,'writes':writes}));raise
"""
    for flag in ('--step-vision-self-check','--step-vision-check'):
        for mode in ('import','runtime'):
            destination=output/('diagnostic_error_'+flag.lstrip('-')+'_'+mode);destination.mkdir()
            child=subprocess.run([sys.executable,'-B','-X','utf8','-c',diagnostic_code,str(root/'tools'),str(fixture),str(destination),flag,mode],
                                 capture_output=True,text=True,encoding='utf-8',timeout=45)
            receipt=destination/'step_vision_error.json'
            data=json.loads(receipt.read_text(encoding='utf-8')) if receipt.is_file() else {}
            facts=json.loads(child.stdout) if child.stdout.strip().startswith('{') else {}
            check('diagnostic_exception_external_only:'+flag+':'+mode,
                  child.returncode==2 and facts.get('exit_code')==2 and facts.get('writes')==[str(receipt.resolve())]
                  and data.get('completed') is False and data.get('game_input') is False and data.get('worker_started') is False
                  and not (fixture/'evidence').exists() and not (fixture/'runtime').exists(),
                  {'stdout':child.stdout,'stderr':child.stderr,'returncode':child.returncode,'receipt':data})


def verify(root=ROOT, output=None, provenance=None, reuse_ocr_cache=None):
    """每个目标实走一次参考/局部路由，输出covered/missing，契约失败才返回非零。"""
    root=Path(root);output=Path(output);output.mkdir(parents=True,exist_ok=True)
    targets=json.loads((root/'assets/step_vision/targets.json').read_text(encoding='utf-8'))
    checks=[];graph=[];reference_rows=[]
    def check(name, condition, details=None):
        """记录真实断言结果；总结果来自断言，不由调用者填写pass标志。"""
        checks.append({'name':name,'passed':bool(condition),'details':details})
    def semantic(value):
        """只排除实测变化的elapsed_ms计时；状态、余额、成本、点位及所有门禁字段仍全量比较。"""
        if isinstance(value,dict):return {k:semantic(v) for k,v in value.items() if k!='elapsed_ms'}
        if isinstance(value,list):return [semantic(v) for v in value]
        return value
    # [验收] 以当前有效catalog逐项核对；历史重复日奖模块不再增加可见步骤。
    from module_catalog import MODULES
    active=[m for m in MODULES if m.worker]
    check('active_catalog_steps',targets['modules']==[m.id for m in active] and len(targets['targets'])==sum(len(m.steps) for m in active))
    check('distinct_goal_specs',all(s['goal'] and s['criteria'] and s['observed_groups'] for s in targets['targets']))
    serialized=json.dumps(targets,ensure_ascii=False)
    check('public_manifest_has_no_private_paths',all(token not in serialized for token in ('E:\\','D:\\','source_path','retained_user_run')))
    check('v85_goddess_semantics',all('叹号' in s['criteria'] and '拍手' in s['criteria'] for s in targets['targets'] if s['module'].startswith('goddess')))

    # [读回] 第二个新解释器禁止调用image_vector，验证构造上下文确实只加载持久向量。
    code="""import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from gamevision_runtime import GameVision
from step_vision import StepVision
from unittest.mock import patch
import onnxruntime as ort
with patch.object(ort.InferenceSession,'run',side_effect=AssertionError('No reference inference in constructor')) as call:
    ctx=StepVision(Path(sys.argv[2]),'navigation')
    print(json.dumps({'loaded':len(ctx.keys),'reference_inferences':ctx.reference_inferences,'session_run_calls':call.call_count}))
    ctx.close()
"""
    child=subprocess.run([sys.executable,'-B','-X','utf8','-c',code,str(root/'tools'),str(root)],capture_output=True,text=True,encoding='utf-8')
    check('fresh_process_loads_without_reference_inference',child.returncode==0 and json.loads(child.stdout)['session_run_calls']==0,child.stdout or child.stderr)
    portable_and_error_checks(root,output,targets,check)
    smoke=self_check(root,output/'self_check')
    check('actual_query_cpu_and_release',smoke['providers']==['CPUExecutionProvider'] and smoke['released'] and smoke['reference_inferences']==0)

    # [原图] 私人映射只读；每张原图核source_sha，路径仅保存在此私人验收输出，不写公开资产。
    originals=json.loads(Path(provenance).read_text(encoding='utf-8-sig')) if provenance else {}
    frames={};payloads={}
    from medium_ocr import MediumOCR
    medium=MediumOCR(root/'models/gamevision_bd2',threads=2)
    ocr_identity=digest(''.join(digest((root/'models/gamevision_bd2'/name).read_bytes()) for name in
                              ('medium_det.onnx','medium_rec.onnx','medium_dictionary.json')).encode())
    ocr_cache=output/'private_ocr_cache';ocr_cache.mkdir(exist_ok=True)
    for index,(key,spec) in enumerate(targets['global_references'].items()):
        shared=root/'assets/shared_references'/spec['file']
        supplemental=bool(spec.get('supplemental'))
        source=(root/'evidence/hunt_goddess_v85/fixtures/failed_goddess_original.png') if supplemental else Path(originals[key]['source']) if key in originals else shared
        raw=source.read_bytes()
        if (key in originals or supplemental) and digest(raw)!=spec['source_sha256']:raise ValueError('Original source hash mismatch: '+key)
        frame=image(source);frames[key]=frame
        cached=ocr_cache/(digest(raw)+'.json')
        seed=Path(reuse_ocr_cache)/cached.name if reuse_ocr_cache else cached
        previous=json.loads(seed.read_text(encoding='utf-8')) if seed.is_file() else {}
        hit=previous.get('model_identity')==ocr_identity and previous.get('source_sha256')==digest(raw)
        payloads[key]=previous['payload'] if hit else medium.read_page(frame)
        if not hit:cached.write_text(json.dumps({'model_identity':ocr_identity,'source_sha256':digest(raw),'payload':payloads[key]},ensure_ascii=False),encoding='utf-8')
        reference_rows.append({'key':key,'reference_sha256':digest(shared.read_bytes()),'original_sha256':digest(raw),
                               'original_verified':key in originals or supplemental,'backend':payloads[key]['backend'],'ocr_cache_hit':hit,'supplemental':supplemental})
        (output/'reference_progress.json').write_text(json.dumps(reference_rows,ensure_ascii=False,indent=2),encoding='utf-8')
        if index%10==0:print('OFFLINE_CPU_ORIGINAL',index+1,key,flush=True)
    check('55_shared_and_approved_supplement_original_reads',sum(not row['supplemental'] for row in reference_rows)==55 and all(r['original_verified'] for r in reference_rows) if provenance else len(reference_rows)>=55)
    check('actual_medium_CPU',medium.session('ocr').get_providers()==['CPUExecutionProvider'] and medium.session('detector').get_providers()==['CPUExecutionProvider'])
    del medium

    # [逐步消费] 同一原图先跑原检查器，再跑生产用wrapper；原合法结果必须保持。
    hooks._root=root
    for module in targets['modules']:
        obj,args_for=adapter(module,root,output/'ocr')
        original=type(obj).inspect;wrapped=hooks.wrap(original,type(obj).__name__)
        hooks._module=module
        for step in [s for s in targets['targets'] if s['module']==module]:
            observations=[]
            for key in step['reference_keys']:
                args=args_for(payloads[key]);kwargs={'include_inventory':False} if module=='equipment' else {}
                base=original(obj,frames[key],*args,**kwargs)
                context_key=(str(root.resolve()),'navigation' if module=='navigation' else module)
                before=sum(_contexts[context_key].counts.values()) if context_key in _contexts else 0
                result=wrapped(obj,frames[key],*args,**kwargs)
                runtime=_contexts.get(context_key)
                use=runtime.trace[-1] if runtime and sum(runtime.counts.values())>before else None
                business_fallback=module!='navigation' and base['state'] in ('page_with_back','page_with_circle_back','page_with_narrow_back','scene_with_home','pass_panel')
                if base['state'] not in ('unknown','other_page') and not business_fallback:
                    check('positive_preserved:'+step['step_id']+':'+key,semantic(result)==semantic(base))
                expected=targets['global_references'][key]['state']
                matched=result['state']==expected or (expected=='equipment_detail' and result['state']=='equipment_reviewed_R_detail')
                # 导航只需原返回契约，不要求把通行证身份细分成业务state；按钮须真实存在。
                nav_alias=(module=='navigation' and expected in ('pass_tasks','pass_levels') and
                           result.get('next_action') in ('click_back','cancel_panel','close_panel') and bool(result.get('return_button')))
                matched=matched or nav_alias
                observations.append({'reference':key,'expected':expected,'base_state':base['state'],'wrapped_state':result['state'],
                                     'matched':matched,'recovered':'step_vision_recovery' in result,
                                     'original_already_valid':base['state']==expected or nav_alias,
                                     'navigation_contract_alias':nav_alias,
                                     'model_candidate_and_local_evidence_used':use is not None,
                                     'model_consumer':use['used'] if use else None,
                                     'stage_ambiguous':use['route']['stage_ambiguous'] if use else None})
            local_only=[name for name,group in step['observed_groups'].items() if name in step['missing_full_states'] and group['local_keys']]
            state_rows=[]
            for state,group in step['observed_groups'].items():
                relevant=[o for o in observations if o['reference']==state or ('|' not in state and o['expected']==state)]
                if any(o['recovered'] and o['matched'] for o in relevant):status='model_recovered'
                elif any(o['matched'] and o['model_candidate_and_local_evidence_used'] for o in relevant):status='model_scoped_validation'
                elif any(o['matched'] for o in relevant):status='original_already_valid'
                elif relevant:status='saved_reference_not_recognized'
                elif group['local_keys']:status='local_evidence_available_but_missing_full_replay'
                else:status='missing_reference_and_local_rule'
                state_rows.append({'state':state,'status':status,'examples':len(relevant),
                                   'local_rule_available':bool(group['local_keys']),'validator_fields':group['fields']})
            fully=all(row['status'] in ('model_recovered','model_scoped_validation','original_already_valid') for row in state_rows)
            graph.append({'step_id':step['step_id'],'goal':step['goal'],'observations':observations,'states':state_rows,
                          'status':'all_declared_states_replayed' if fully else 'partially_replayed_or_missing','fully_covered':fully,
                          'missing_full_states':step['missing_full_states'],'local_only_available':local_only,
                          'missing_local_states':[n for n,g in step['observed_groups'].items() if g['missing_local_rule']],
                          'criteria':step['observed_groups']})
        print('OFFLINE_MODULE_DONE',module,flush=True)
        (output/'graph_progress.json').write_text(json.dumps(graph,ensure_ascii=False,indent=2),encoding='utf-8')

    # [真实恢复] 清空每日页的OCR让原检查器失败，再验证生产wrapper真实Medium重读被返回消费。
    hooks._module='task_rewards'
    obj,args_for=adapter('task_rewards',root,output/'recovery')
    original=type(obj).inspect;wrapped=hooks.wrap(original,type(obj).__name__)
    empty={'width':1280,'height':720,'language':'zh-Hans-CN','lines':[]}
    key='daily_tasks|daily';base=original(obj,frames[key],empty);recovered=wrapped(obj,frames[key],empty)
    check('actual_targeted_medium_result_consumed',base['state']=='unknown' and recovered['state']=='daily_tasks' and 'step_vision_recovery' in recovered,{'base':base['state'],'after':recovered['state']})
    # 同样全页几乎不变，但关键标题抹除；高分模型不能补造标题或允许领奖。
    negative=frames[key].copy();negative[15:55,175:285]=0
    denied=wrapped(obj,negative,empty)
    check('high_global_similarity_without_title_cannot_claim',denied['state']=='unknown')
    # 对照每日与每周：目标OCR分开读实际标题，不因同一task_page族而混用身份。
    weekly=wrapped(obj,frames['weekly_tasks|weekly'],empty)
    check('daily_weekly_identity_independent',recovered['state']=='daily_tasks' and weekly['state']=='weekly_tasks')
    # [真实消费者] 只把首轮导航输出注入unknown，后续使用真实opening/局部控件，不mock最终好状态。
    from common_navigation import PageDiagnosis
    page=PageDiagnosis();hooks._module='navigation'
    def initial_unknown(self, frame):
        """模拟旧第一轮识图失败；原控件对象与validator全部保留真实实现。"""
        return {'state':'unknown','next_action':'wait'}
    nav_recovered=hooks.wrap(initial_unknown,'PageDiagnosis')(page,frames['main_menu'])
    check('navigation_complete_real_validator_consumed',nav_recovered.get('state')=='main_menu' and nav_recovered.get('next_action')=='done')
    black=np.zeros_like(frames['main_menu'])
    nav_denied=hooks.wrap(initial_unknown,'PageDiagnosis')(page,black)
    check('navigation_missing_controls_preserves_wait_interface',nav_denied=={'state':'unknown','next_action':'wait'})
    # [缓存接线] 生产导航identify也必须使用55持久向量，而不只是新StepVision构造器。
    from gamevision_runtime import GameVision
    from step_vision import context
    from queue_recognition import RecognitionContext
    context(root,'navigation')
    vector_calls=[];native_vector=GameVision.image_vector
    def counted_vector(engine, frame):
        """计数实际query推理，预热身份校验后再替换，避免改变被测预处理签名。"""
        vector_calls.append(True);return native_vector(engine,frame)
    hooks.install(root,'navigation')
    navigation=RecognitionContext()
    with patch.object(GameVision,'image_vector',counted_vector):
        identified=navigation.identify(frames['main_menu'])
    check('production_navigation_reuses_55_vectors',identified.get('available') and len(navigation.references)==55 and len(vector_calls)==1)
    from m02_free_draw import FreeDrawRecognizer,FreeDrawController
    free=FreeDrawRecognizer();free_negative=frames['free_confirmation'].copy();free_negative[355:410,585:675]=0
    fresult=free.inspect(free_negative);decision=FreeDrawController().step(fresult,0)
    check('missing_free_control_never_authorizes_confirm',fresult['state']!='free_confirmation' and (decision or {}).get('action')!='confirm_free_draw')

    # [安全后备] 模拟清单/模型/参考/预处理/缓存故障，不改真实模型文件，验证老合法结果保持。
    # 先保存实际逐步路由原始摘要，再进行故障注入；这些candidate_step集合不等于步骤完成。
    hooks.uninstall(output)
    hooks._module='task_rewards'
    legal=original(obj,frames[key],payloads[key])
    for reason in ('disabled','model_error','cache_invalid'):
        with patch('step_vision.context',side_effect=ValueError(reason)):
            check('fallback_preserves_original:'+reason,wrapped(obj,frames[key],payloads[key])==legal)
    real_read=Path.read_bytes
    chosen_ref=root/'assets/shared_references'/targets['global_references']['main_menu']['file']
    for name,changed in [('model',root/'models/gamevision_bd2/visual.onnx'),('reference',chosen_ref),('vector_cache',root/'assets/step_vision/vectors.npz')]:
        def changed_read(p, chosen=changed):
            """只替换测试调用所见字节，磁盘文件始终不改。"""
            raw=real_read(p)
            return raw+b'identity_test' if p==chosen else raw
        with patch.object(Path,'read_bytes',changed_read):
            try:StepVision(root,'navigation')
            except ValueError:check('changed_'+name+'_rejects_cache',True)
            else:check('changed_'+name+'_rejects_cache',False)
    import prepare_step_vision
    real_text=Path.read_text
    def altered_manifest(p, *args, **kwargs):
        """仅给本次读取的清单加入字段，验证清单变更不能复用旧向量。"""
        text=real_text(p,*args,**kwargs)
        if p==root/'assets/step_vision/targets.json':
            value=json.loads(text);value['changed_for_test']=True;return json.dumps(value)
        return text
    with patch.object(Path,'read_text',altered_manifest):
        try:StepVision(root,'navigation')
        except ValueError:check('manifest_change_rejects_cache',True)
        else:check('manifest_change_rejects_cache',False)
    with patch.object(prepare_step_vision,'PREPROCESS','changed_schema'):
        try:StepVision(root,'navigation')
        except ValueError:check('preprocess_change_rejects_cache',True)
        else:check('preprocess_change_rejects_cache',False)
    with patch('model_ocr_provider._settings',return_value=None):
        try:StepVision(root,'navigation')
        except ValueError:check('model_disabled_rejects_optional_context',True)
        else:check('model_disabled_rejects_optional_context',False)

    # [有界回执] 累计计数保留全部次数，最近原始记录最多64条；release只释放本层模型。
    runtime=StepVision(root,'navigation')
    for index in range(100):runtime.record('bounded_test',{'step_ids':['navigation.01']},{'state':'unknown'})
    check('trace_is_bounded_and_counts_durable',len(runtime.trace)==64 and runtime.counts['bounded_test']==100)
    runtime.close();check('contexts_release',runtime.engine is None and runtime.medium is None)
    check('all_active_targets_reported_without_empty_coverage',len(graph)==sum(len(m.steps) for m in active) and all(row['status'] and row['criteria'] for row in graph))
    source_models=output.parent/'model_sources.json'
    if source_models.is_file():
        identity=json.loads(source_models.read_text(encoding='utf-8'))
        check('11_model_files_unchanged',len(identity['files'])==11 and all(digest((root/'models/gamevision_bd2'/r['file']).read_bytes())==r['sha256'] for r in identity['files']))
    from collections import Counter
    state_counts=dict(Counter(state['status'] for step in graph for state in step['states']))
    fully_covered=all(step['fully_covered'] for step in graph)
    report={'checks':checks,'passed':sum(c['passed'] for c in checks),'total':len(checks),
            'graph':graph,'references':reference_rows,'game_input':False,'worker_started':False,
            'fully_covered':fully_covered,'state_counts':state_counts,
            'medium_fullpage_inferences_this_run':sum(not row['ocr_cache_hit'] for row in reference_rows),
            'medium_fullpage_bound_cache_hits':sum(row['ocr_cache_hit'] for row in reference_rows),
            'acceptance_scope':'全部声明步骤/状态如实列出；原生通过、模型局部验证、模型恢复及缺图分开，不代表所有步骤模型均已恢复',
            'scope':'真实保存图CPU重读/生产包装/原识图返回；参考已复用，不是独立泛化或实机验收'}
    (output/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'total':report['total'],
                      'fully_replayed_steps':sum(row['fully_covered'] for row in graph),'fully_covered':fully_covered,
                      'state_counts':state_counts,'steps':len(graph)},ensure_ascii=False),flush=True)
    return 0 if report['passed']==report['total'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--provenance',type=Path);parser.add_argument('--reuse-ocr-cache',type=Path)
    args=parser.parse_args();raise SystemExit(verify(args.root,args.output,args.provenance,args.reuse_ocr_cache))
