"""按当前模块/画面阶段使用持久参考，路由局部UI复核和CPU OCR；不产生游戏动作。"""
import atexit
import copy
import hashlib
import json
from collections import Counter, deque
from pathlib import Path
import cv2
import numpy as np

from prepare_step_vision import digest, fingerprint


class StepVision:
    """[功能] 只接受身份绑定缓存；全页候选须经过原始局部UI验证才允许请求OCR恢复。"""
    def __init__(self, root, module):
        """读取清单及已算向量；不重算55张参考。会话在第一次查询时才加载。"""
        self.root=Path(root);self.module=module;self.engine=None;self.medium=None
        self.reference_inferences=0;self.query_inferences=0;self.recoveries=0;self.trace=deque(maxlen=64)
        self.counts=Counter();self.step_hits=Counter()
        self.memo={};self.closed=False
        folder=self.root/'assets/step_vision'
        self.targets=json.loads((folder/'targets.json').read_text(encoding='utf-8'))
        if self.targets.get('enabled') is not True:raise ValueError('Step model disabled')
        from model_ocr_provider import _settings
        if _settings(self.root) is None:raise ValueError('Embedded model disabled')
        self.config=json.loads((self.root/'config/model_recognition.json').read_text(encoding='utf-8-sig'))
        self.cache=json.loads((folder/'cache.json').read_text(encoding='utf-8'))
        if self.cache['fingerprint']!=fingerprint(self.root,self.targets):raise ValueError('Step cache identity changed')
        raw=(folder/'vectors.npz').read_bytes()
        if digest(raw)!=self.cache['cache_sha256']:raise ValueError('Step vector cache changed')
        with np.load(folder/'vectors.npz',allow_pickle=False) as data:
            self.keys=data['keys'].tolist();self.vectors=data['vectors'].copy()
        if (self.vectors.ndim!=2 or len(self.keys)!=len(self.vectors) or not np.isfinite(self.vectors).all()
                or len(set(self.keys))!=len(self.keys)):
            raise ValueError('Invalid step vector cache')
        self.index={key:i for i,key in enumerate(self.keys)}
        self.steps=[s for s in self.targets['targets'] if s['module']==module]
        self.locals={}
        for step in self.steps:
            for key in step['local_keys']:
                spec=self.targets['locals'][key]
                self.locals[key]=(spec,cv2.imdecode(np.frombuffer((self.root/key).read_bytes(),np.uint8),cv2.IMREAD_COLOR))

    def close(self):
        """释放本上下文的ONNX会话与内存查询缓存；不关闭Ollama或其他服务。"""
        self.engine=None;self.medium=None;self.memo.clear();self.closed=True

    def record(self, used, route, result, accepted=None):
        """保留有界原始命中与累计计数；共享阶段只记候选步骤集合，不声称已推进。"""
        self.counts[used]+=1
        for key in route.get('step_ids',[]):self.step_hits[key]+=1
        self.trace.append({'route':route,'used':used,'result_state':result.get('state'),'accepted':accepted})

    def _vector(self, image):
        """只对当前查询图/局部ROI推理；持久参考不会重复计算。"""
        if self.closed:raise RuntimeError('Step context released')
        if self.engine is None:
            from gamevision_runtime import GameVision
            self.engine=GameVision(self.root/'models/gamevision_bd2',threads=self.config.get('threads',2))
        self.query_inferences+=1
        return self.engine.image_vector(image)

    def _local_evidence(self, normalized, permitted):
        """对模型候选限定的原始UI组做形状、亮度和真实CPU局部向量复核。"""
        local=[]
        for local_key in sorted(permitted):
            anchor,template=self.locals[local_key]
            a,b,c,d=anchor['box']
            if not (0<=a<c<=1280 and 0<=b<d<=720):continue
            patch=normalized[b:d,a:c]
            if patch.shape!=template.shape:continue
            gray=cv2.cvtColor(template,cv2.COLOR_BGR2GRAY)
            current=cv2.cvtColor(patch,cv2.COLOR_BGR2GRAY)
            feature_patch=patch
            if anchor['folder']=='ui_foreground':
                # 原主页参考是二值前景；查询按原生成阈值抽取白色字形，不能与壁纸RGB直接比。
                spread=patch.max(2).astype(np.int16)-patch.min(2).astype(np.int16)
                current=np.where((current>=anchor['gray_threshold'])&(spread<=anchor['maximum_channel_spread']),255,0).astype(np.uint8)
                feature_patch=cv2.cvtColor(current,cv2.COLOR_GRAY2BGR)
            if gray.std()<3 or current.std()<3:continue
            correlation=float(cv2.matchTemplate(current,gray,cv2.TM_CCOEFF_NORMED)[0,0])
            ink=gray>max(70,float(np.percentile(gray,70)))
            if not np.isfinite(correlation) or correlation<.90 or not ink.any():continue
            if float(current[ink].mean())<.8*float(gray[ink].mean()):continue
            similarity=float(self._vector(feature_patch)@self.vectors[self.index['local:'+local_key]])
            if similarity>=.94:
                local.append({'key':local_key,'box':anchor['box'],'correlation':correlation,'similarity':similarity})
        return local

    def route(self, image, state='unknown'):
        """候选限定当前模块和实际阶段；原始ROI形状/亮度/向量均通过后才返回恢复目标。"""
        # [分支] 用户运行中关闭或修改模型配置，立即退回原识图，不继续使用旧上下文。
        current=json.loads((self.root/'config/model_recognition.json').read_text(encoding='utf-8-sig'))
        if current.get('enabled') is not True or current!=self.config:return None
        if image is None or image.ndim!=3:return None
        h,w=image.shape[:2]
        if w<640 or h<360 or abs(w/h-16/9)>.035:return None
        normalized=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
        memo_key=(hashlib.sha256(normalized.tobytes()).hexdigest(),state)
        if memo_key in self.memo:return self.memo[memo_key]
        allowed={key for step in self.steps for key in step['reference_keys']}
        if state not in ('unknown','other_page'):
            allowed={key for key in allowed if self.targets['global_references'][key]['state']==state}
        if not allowed:return self.local_route(normalized,state)
        query=self._vector(normalized)
        scored=sorted(((float(query@self.vectors[self.index['global:'+key]]),key) for key in allowed),reverse=True)
        score,key=scored[0]
        if score<.90:return self.local_route(normalized,state)
        spec=self.targets['global_references'][key]
        steps=[s for s in self.steps if key in s['reference_keys']]
        # [校验] 全页高相似不够：逐个对同位置局部前景检查，并实际计算局部模型向量。
        groups=[group for step in steps for name,group in step['observed_groups'].items()
                if name==key or ('|' not in name and name==spec['state'])]
        permitted={k for group in groups for k in group['local_keys']}
        local=self._local_evidence(normalized,permitted)
        control=next((group.get('existing_control_validator') for group in groups if group.get('existing_control_validator')),None) if self.module=='navigation' else None
        if not local and not control:return None
        result={'module':self.module,'state':spec['state'],'reference':key,'score':score,
                'step_ids':[s['step_id'] for s in steps], 'stage_ambiguous':len(steps)!=1,
                'locals':local,'ocr_rois':[list(box) for box in sorted({tuple(box) for group in groups for box in group['ocr_rois']})],
                'stage_status':'shared_state_stage_unknown' if len(steps)!=1 else 'single_observed_stage',
                'existing_control_validator':control,
                'global_masked':True,'local_source':'original_in_memory_UI'}
        # [状态] 只缓存有限数量的当前帧路由；不靠点击次数推进步骤或写账本。
        if len(self.memo)>=8:self.memo.pop(next(iter(self.memo)))
        self.memo[memo_key]=result
        return result

    def local_route(self, normalized, state):
        """缺全页参考仍可用真实局部模板提出候选；最后必须交原validator，不能直接通行。"""
        candidates={}
        for step in self.steps:
            for name,group in step['observed_groups'].items():
                if state not in ('unknown','other_page') and name.split('|')[0]!=state:continue
                if group['local_keys']:candidates[name]=group
        best=None
        for name,group in candidates.items():
            local=self._local_evidence(normalized,set(group['local_keys']))
            if not local:continue
            score=max(item['similarity'] for item in local)
            # 图标/标题候选只用于OCR或原局部检查；多个阶段同图时明确记共享，不推进步骤。
            if best is None or score>best['score']:
                steps=[s['step_id'] for s in self.steps if name in s['observed_groups']]
                best={'module':self.module,'state':name.split('|')[0],'reference':None,'score':score,
                      'step_ids':steps,'stage_ambiguous':len(steps)!=1,'locals':local,
                      'ocr_rois':group['ocr_rois'],'global_masked':True,
                      'local_source':'original_in_memory_UI','stage_status':'local_candidate_requires_original_validator',
                      'route_kind':'local_only_no_full_reference'}
        return best

    def targeted_ocr(self, image, route, payload):
        """按模型选中的局部UI与页面文字区做Medium CPU读取，坐标换回原完整客户端。"""
        from medium_ocr import MediumOCR
        if self.medium is None:self.medium=MediumOCR(self.root/'models/gamevision_bd2',threads=self.config.get('threads',2))
        normalized=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
        result=copy.deepcopy(payload) if isinstance(payload,dict) else {'width':1280,'height':720,'lines':[]}
        # 原payload可能是非1280分辨率；把新OCR坐标按其实际宽高回映射，不修改旧词框。
        width,height=result.get('width',1280),result.get('height',720)
        added=[]
        boxes=[item['box'] for item in route['locals'][:4]]+route['ocr_rois']
        for a,b,c,d in boxes:
            patch=normalized[b:d,a:c]
            if d-b<=100:
                prediction=self.medium.read_text(patch)
                rows=([{'text':prediction['text'],'words':[{'text':prediction['text'],'x':0,'y':0,'width':c-a,'height':d-b}]}]
                      if prediction['score']>=.85 and prediction['text'].strip() else [])
            else:rows=self.medium.read_page(patch)['lines']
            for row in rows:
                item=copy.deepcopy(row)
                for word in item['words']:
                    word['x']=(word['x']+a)*width/1280;word['y']=(word['y']+b)*height/720
                    word['width']*=width/1280;word['height']*=height/720
                added.append(item)
        # [输出] 这是独立的目标重读payload，不覆盖调用方原payload；不合并重复新旧行。
        # 包装器仅在未知页采用它重判；成本/余额仍须上游inspect_frame的独立读数与原门禁。
        result['width']=width;result['height']=height;result['lines']=added
        self.recoveries+=1
        return result


_contexts={}


def context(root, module):
    """每worker/模块复用上下文；关闭后不复用旧会话。"""
    key=(str(Path(root).resolve()),module)
    if key not in _contexts:_contexts[key]=StepVision(root,module)
    return _contexts[key]


def release():
    """worker结束或解释器退出时释放本层全部CPU模型。"""
    for value in _contexts.values():value.close()
    _contexts.clear()


atexit.register(release)


def self_check(root, output, image=None):
    """离线入口供源码/冻结双验：真实读缓存并查询图，报告CPU与释放，不发现窗口或运行worker。"""
    root=Path(root);output=Path(output);output.mkdir(parents=True,exist_ok=True)
    runtime=StepVision(root,'navigation')
    try:
        if image is None:
            spec=runtime.targets['global_references']['main_menu']
            image=root/'assets/shared_references'/spec['file']
        frame=cv2.imdecode(np.frombuffer(Path(image).read_bytes(),np.uint8),cv2.IMREAD_COLOR)
        route=runtime.route(frame)
        report={'fingerprint':runtime.cache['fingerprint'],'cache_identity_verified':True,
                'loaded_vectors':len(runtime.keys),'global_count':runtime.cache['global_count'],
                'local_count':runtime.cache['local_count'],'reference_inferences':runtime.reference_inferences,
                'query_inferences':runtime.query_inferences,
                'providers':runtime.engine.session('visual').get_providers() if runtime.engine else [],
                'route':route,'game_input':False,'worker_started':False,'runtime_source_files_required':False}
        if report['providers']!=['CPUExecutionProvider'] or runtime.reference_inferences!=0:
            raise RuntimeError('CPU cache self-check failed')
    finally:
        runtime.close()
    report['released']=runtime.engine is None and runtime.medium is None and not runtime.memo
    (output/'step_vision_self_check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report
