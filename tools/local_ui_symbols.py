"""在受限界面区域内核对原始图形；只返回观察结果，不发送游戏输入。"""
import cv2
import numpy as np
import atexit
import json
import time
import weakref

_recoveries=weakref.WeakSet()


def release_local_symbol_models():
    """worker退出时释放CPU会话及两组参考向量，不启动模型服务或GPU runner。"""
    for item in list(_recoveries):
        item.engine=None
        item.references.clear()


atexit.register(release_local_symbol_models)


class LocalSymbolRecovery:
    """现有visual.onnx只提出局部两类候选；原图相关核验独立决定是否采用。"""
    def __init__(self,root):
        self.root=root
        self.engine=None
        self.references={}
        _recoveries.add(self)

    def recover(self,normalized,group,templates,specs):
        """固定邻域粗网格给class/box，再仅精核该类附近4像素；关闭或异常返回CV后备。"""
        started=time.perf_counter()
        try:
            settings=json.loads((self.root/'config/model_recognition.json').read_text(encoding='utf-8-sig'))
            if settings.get('enabled') is not True:
                return {'model_candidate_used':False,'reason':'model_disabled'}
            if group not in ('resource','goddess') or len(templates)!=2:
                raise ValueError('Only two bounded icon groups are supported')
            if self.engine is None:
                from gamevision_runtime import GameVision
                self.engine=GameVision(self.root/'models/gamevision_bd2',threads=2)
            if group not in self.references:
                self.references[group]={name:self.engine.image_vector(image) for name,image in templates.items()}
            ranked=[]
            for name,template in templates.items():
                a,b,c,d=specs[name]['box'] if 'box' in specs[name] else specs[name]['box_1280']
                h,w=template.shape[:2];best=(-2.,None)
                for y in range(b-9,b+10,3):
                    for x in range(a-9,a+10,3):
                        patch=normalized[y:y+h,x:x+w]
                        if patch.shape[:2]!=(h,w):continue
                        score=float(self.engine.image_vector(patch)@self.references[group][name])
                        if score>best[0]:best=(score,[x,y,x+w,y+h])
                ranked.append((best[0],name,best[1]))
            ranked.sort(reverse=True)
            top,name,box=ranked[0]
            accepted=top>=.80 and top-ranked[1][0]>=.20
            result={'model_candidate_used':False,'model_attempted':True,'backend':'onnx_cpu_visual_local',
                    'class':name,'score':top,'margin':top-ranked[1][0],'box':box}
            if accepted:
                # 模型输出实质收窄为一个类和一个小框；不再按模型得分直接授权操作。
                refined=match_local_icon(normalized,templates[name],box,4)
                if refined['score']>=.85:
                    result.update(model_candidate_used=True,glyph_score=refined['score'],point=refined['point'])
            result['elapsed_ms']=(time.perf_counter()-started)*1000
            return result
        except Exception as error:
            # 这里只包装无副作用的本地推理；宿主停止、日期和消费门禁不在此异常范围内。
            return {'model_candidate_used':False,'model_attempted':True,'reason':'model_unavailable',
                    'error_type':type(error).__name__,'elapsed_ms':(time.perf_counter()-started)*1000}


def match_local_icon(normalized, template, box, padding=12, scales=(.9, 1., 1.1)):
    """输入规范画面和真实裁片，在原位置附近查找；输出最佳相关分数与中心。"""
    a, b, c, d = box
    left, top = max(0, a-padding), max(0, b-padding)
    region = cv2.cvtColor(normalized[top:min(720,d+padding),left:min(1280,c+padding)], cv2.COLOR_BGR2GRAY)
    gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    best = {'score': -1., 'point': None}
    for scale in scales:
        patch = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        h, w = patch.shape
        if h > region.shape[0] or w > region.shape[1]:
            continue
        _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(region, patch, cv2.TM_CCOEFF_NORMED))
        if score > best['score']:
            best = {'score': float(score), 'point': [left+point[0]+w/2, top+point[1]+h/2]}
    return best


def goddess_button(normalized, images, specs, recovery=None, legacy_kind=None):
    """只核对右下F圆按钮两种图形和圆形几何；不把其他位置的叹号当入口。"""
    region = cv2.cvtColor(normalized[558:622,908:976], cv2.COLOR_BGR2GRAY)
    circles = cv2.HoughCircles(region, cv2.HOUGH_GRADIENT, 1, 20,
                               param1=80, param2=13, minRadius=18, maxRadius=31)
    circle = None if circles is None else next((v for v in circles[0]
        if abs(float(v[0])-33)<6 and abs(float(v[1])-33)<6), None)
    # 两图形始终只在F按钮原区域匹配；圆框/F键残留但图形不清时属于遮挡或过渡。
    matches = {name: match_local_icon(normalized, images[name], specs[name]['box_1280'], 5)
               for name in ('goddess_hand', 'goddess_reward_ready')}
    # [按键兼容] 当前截图的F字形另存可追溯小图，仍以原.90阈值核验，不放宽圆形或拍手门禁。
    key=max((match_local_icon(normalized,images[name],specs[name]['box_1280'],3)
             for name in ('goddess_f_key','goddess_f_key_legacy','goddess_f_key_v95') if name in images),key=lambda value:value['score'])
    if circle is None:
        # 不记忆地板纹理。控件和F键均消失才暴露候选；背景边缘密度只作诊断，不能把正常广场纹理误作遮挡。
        # 该候选还须上游正确场景门禁、上游明确场景门禁和连续5秒共同使用，不能单独核销任何动作。
        area=cv2.cvtColor(normalized[560:640,914:970],cv2.COLOR_BGR2GRAY)
        edges=float(np.mean(cv2.Canny(area,60,120)>0))
        disappeared=bool(max(v['score'] for v in matches.values())<.65 and key['score']<.70)
        return {'kind':None,'button_verified':False,'control_state':'absent' if disappeared else 'obscured',
                'scores':matches,'f_key_score':key['score'],'local_edge_density':edges}
    if key['score']<.90:
        return {'kind':None,'button_verified':False,'control_state':'obscured','scores':matches}
    ranked = sorted(matches, key=lambda name: matches[name]['score'], reverse=True)
    first, second = ranked
    kind = first if matches[first]['score'] >= .85 and matches[first]['score']-matches[second]['score'] >= .08 else None
    model={'model_candidate_used':False}
    if recovery is not None and (not kind or legacy_kind!=kind):
        model=recovery.recover(normalized,'goddess',{name:images[name] for name in matches},specs)
        if model.get('model_candidate_used'):
            kind=model['class']
    return {'kind': kind, 'button_verified': True,'control_state':'visible' if kind else 'obscured','scores':matches,
            # 圆心已限定在规范中心6像素内、半径至少18，统一取安全内部点避免fresh重核受1像素抖动影响。
            'point':[941.,591.],'detected_center':[908+float(circle[0]),558+float(circle[1])],'model_candidate':model}
