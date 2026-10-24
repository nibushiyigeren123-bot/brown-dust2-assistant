"""worker统一识图包装：候选只路由局部校验/OCR，再交原检查器决定；无输入权限。"""
import functools
import importlib
import os
import threading
import json
import weakref
from collections import Counter
from pathlib import Path

_installed=[]
_guard=threading.local()
_root=None
_module=None
_stats=Counter()
_queue_contexts=weakref.WeakSet()

HOOKS=(('common_navigation','PageDiagnosis'),('m01_management','ManagementRecognizer'),
       ('m02_free_draw','FreeDrawRecognizer'),('daily_vision','DailyVision'),
       ('phase08_vision','Phase08Vision'),('equipment_vision','EquipmentVision'),
       ('event_battle_vision','EventBattleVision'),('pass_reward_vision','PassRewardVision'),
       ('task_reward_vision','TaskRewardVision'))


def local_validator(obj, class_name, image, route, base):
    """模型选择局部原判据，结果参与最终观察；不通过时不能新增任何可动作状态。"""
    state=route['state']
    if class_name=='PageDiagnosis':
        # [恢复] 候选选择完整原控件validator；只使用其真实点位和接口字段，不从模型造按钮。
        opening=obj.opening.recognize(image)
        if state=='main_menu':
            if opening.get('state')=='main_menu':
                return {'state':'main_menu','next_action':'done','opening':opening}
        elif state=='scene_with_home':
            home=obj.find_home(image) if opening.get('state')!='loading_or_unknown_overlay' else None
            if home:return {'state':'scene_with_home','next_action':'click_home','home_button':home}
        elif state=='reward_overlay':
            overlay=obj.reward_overlay.inspect(image)
            if overlay:
                from reward_overlay_vision import reward_dismiss_point
                h,w=image.shape[:2]
                return {**overlay,'next_action':'close_panel','return_button':{'client_point':reward_dismiss_point(w,h),
                        'normalized_point':reward_dismiss_point(),'scores':overlay['visual_scores']}}
        elif state in ('signin_event','signin_pass'):
            if opening.get('state')==state and opening.get('next_action')=='click_blank':
                return {'state':state,'next_action':'dismiss_opening','opening':opening}
        else:
            returned=obj.returns.inspect(image)
            if returned and returned['state']==state:return {**returned,'opening':opening}
        return base
    elif class_name=='ManagementRecognizer':
        if state=='management_panel':
            panel=obj.panel(image)
            if panel is not None:return {'state':'management_panel','panel':panel}
            verified=False
        elif state=='reward':
            verified=obj._match(image,obj.reward,(.34,.20,.67,.48),source_width=2560,threshold=.72,foreground_only=True) is not None
        else:
            from ui_foreground import MenuForegroundRecognizer
            verified=MenuForegroundRecognizer().inspect(image)['state']=='main_menu'
    else:
        import cv2
        gray=cv2.cvtColor(cv2.resize(image,(1280,720)),cv2.COLOR_BGR2GRAY)
        # [分支] 确认框/结果/动画/两类卡池各自走原有match边界，不能拿全页分数替控件。
        checks={'free_confirmation':[('confirm_title',(570,274,710,325),.80),('free_word',(592,360,669,408),.85)],
                'draw_result':[('back',(90,7,139,63),.74)],
                'draw_animation':[('skip_light',(1158,18,1206,62),.84)],
                'gacha_page':[('paid_label',(750,615,878,669),.77),('costume_tab',(88,207,147,257),.76),('equipment_tab',(88,280,147,330),.76)]}
        if state=='main_menu':verified=obj.navigation.opening.menu.inspect(image)['state']=='main_menu'
        elif state in checks:
            verified=all(obj.match(gray,name,roi,threshold=threshold) is not None for name,roi,threshold in checks[state])
            if state=='draw_animation' and not verified:
                verified=obj.match(gray,'skip_dark',(1158,18,1206,62),threshold=.84,foreground=True) is not None
        else:verified=False
    # 原检查器仍提供具体免费、颜色、徽章、坐标等结果；局部validator不能补造这些字段。
    if verified:return base
    if base.get('state') in ('unknown','other_page'):return base
    return {'state':'unknown','reason':'step_selected_local_controls_not_confirmed'}


def wrap(original, class_name):
    """保留原函数签名及所有独立门禁；仅未知页且局部证据成立才尝试目标OCR恢复。"""
    @functools.wraps(original)
    def inspect(self, image, *args, **kwargs):
        """一次顶层识图只路由一次，嵌套原检查器不会重复包装或递归恢复。"""
        if getattr(_guard,'active',False):return original(self,image,*args,**kwargs)
        _guard.active=True
        try:
            base=original(self,image,*args,**kwargs)
            try:
                from step_vision import context
                module=_module or os.environ.get('BD2_QUEUE_TASK')
                if class_name=='PageDiagnosis':module='navigation'
                runtime=context(_root,module)
                # [语义] 通用返回控件不是业务页面身份；仅这些导航后备状态允许目标OCR继续复核。
                # main_menu和任何合法业务state均保留原观察，加载/弹层也不借此穿透。
                semantic_unknown=base.get('state') in ('unknown','other_page')
                if class_name not in ('PageDiagnosis','ManagementRecognizer','FreeDrawRecognizer'):
                    semantic_unknown=semantic_unknown or base.get('state') in (
                        'page_with_back','page_with_circle_back','page_with_narrow_back','scene_with_home','pass_panel')
                route=runtime.route(image,'unknown' if semantic_unknown else base.get('state','unknown'))
                if not route:
                    _stats['no_candidate:'+str(module)+':'+base.get('state','unknown')]+=1
                    return base
                # [调用] 不支持OCR参数的纯模板检查器用模型所选局部验证后重调原检查器。
                # 模型不修正它们的坐标/完成结果；真实动作仍由原检查器和输入前重读决定。
                if class_name in ('PageDiagnosis','ManagementRecognizer','FreeDrawRecognizer'):
                    if base.get('state') not in ('unknown','other_page'):
                        runtime.record('known_original_preserved_no_correction',route,base)
                        return base
                    result=local_validator(self,class_name,image,route,base)
                    runtime.record('model_selected_original_local_validator',route,result)
                    return result
                if not semantic_unknown:
                    runtime.record('scoped_anchor_validation_existing_observation',route,base)
                    return base
                payload=args[0] if args else kwargs.get('payload')
                if class_name=='EquipmentVision':payload=None
                recovered=runtime.targeted_ocr(image,route,payload)
                if class_name=='EquipmentVision':
                    from daily_vision import OcrDocument
                    replacement=OcrDocument(recovered)
                else:replacement=recovered
                new_args=(replacement,*args[1:]) if args else args
                new_kwargs=dict(kwargs)
                if not args:new_kwargs['payload']=replacement
                # [消费] 恢复后的真实OCR重新进入原检查器，其状态直接返回原控制器。
                result=original(self,image,*new_args,**new_kwargs)
                accepted=result.get('state')==route['state']
                runtime.record('targeted_medium_reinspection',route,result,accepted)
                if accepted:
                    return dict(result,step_vision_recovery={'module':module,'steps':route['step_ids'],
                                                           'source':'CPU_targeted_OCR_then_original_validator'})
                return base
            except Exception as error:
                _stats['fallback:'+type(error).__name__]+=1
                from execution_control import ExecutionControlError
                if isinstance(error,ExecutionControlError):raise
                # [异常] 禁用、缓存失效、模型失败均回到原合法观察；不放宽也不阻断它。
                return base
        finally:
            _guard.active=False
    return inspect


def install(root, module=None):
    """显式安装统一包装，无模型推理、无游戏输入；多次调用不叠加包装。"""
    global _root,_module
    _root=Path(root);_module=module
    if _installed:return
    try:
        for name,class_name in HOOKS:
            cls=getattr(importlib.import_module(name),class_name)
            original=cls.inspect
            cls.inspect=wrap(original,class_name)
            _installed.append((cls,'inspect',original))
        # [缓存接线] 原导航context也用同一已验55向量，避免新worker又在旧导航路径重算55张。
        # 只改变参考初始化；原identify的query、场景候选与独立退出控件判据保持。
        from queue_recognition import RecognitionContext
        original_identify=RecognitionContext.identify
        @functools.wraps(original_identify)
        def cached_identify(obj, image):
            """身份有效时注入持久参考；异常/禁用完整回到原导航初始化流程。"""
            if obj.references is None:
                try:
                    from step_vision import context
                    from gamevision_runtime import GameVision
                    from frame_store import private_mask
                    import cv2,numpy as np
                    runtime=context(_root,'navigation')
                    manifest=json.loads((_root/'assets/shared_references/index.json').read_text(encoding='utf-8'))
                    references={key:runtime.vectors[runtime.index['global:'+key]] for key in manifest['templates']}
                    images={key:private_mask(cv2.imdecode(np.frombuffer((_root/'assets/shared_references'/spec['file']).read_bytes(),np.uint8),cv2.IMREAD_COLOR))
                            for key,spec in manifest['templates'].items() if spec.get('navigation')}
                    engine=GameVision(_root/'models/gamevision_bd2',threads=runtime.config.get('threads',2))
                    obj.manifest,obj.references,obj.images,obj.engine=manifest,references,images,engine
                    _queue_contexts.add(obj);_stats['navigation_reference_cache_loaded']+=1
                except Exception as error:
                    _stats['navigation_cache_fallback:'+type(error).__name__]+=1
            return original_identify(obj,image)
        RecognitionContext.identify=cached_identify
        _installed.append((RecognitionContext,'identify',original_identify))
    except Exception as error:
        # [异常] 安装中断恢复已替换方法；可选识图层不能使原worker无法启动。
        for cls,method,original in reversed(_installed):setattr(cls,method,original)
        _installed.clear();_stats['install_fallback:'+type(error).__name__]+=1
        return False
    return True


def uninstall(output=None):
    """恢复原识图方法并释放本层模型，供worker结束和离线测试隔离使用。"""
    for cls,method,original in reversed(_installed):setattr(cls,method,original)
    _installed.clear()
    from step_vision import release, _contexts
    if output is not None:
        # [输出] worker结束保存实际命中/未知/后备摘要与最近64条原始路由，不永久保存画面。
        report={'fallback':dict(_stats),'contexts':[{'module':c.module,'counts':dict(c.counts),
                'candidate_step_hits_not_completion':dict(c.step_hits),'recent_trace':list(c.trace),'reference_inferences':c.reference_inferences,
                'query_inferences':c.query_inferences,'recoveries':c.recoveries} for c in _contexts.values()]}
        try:(Path(output)/'step_vision_usage.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        except OSError:pass
    release()
    for obj in list(_queue_contexts):
        obj.engine=None;obj.references=None;obj.images.clear()
    _queue_contexts.clear()
    _stats.clear()
