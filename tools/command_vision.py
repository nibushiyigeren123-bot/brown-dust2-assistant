"""Embedded trained page candidates and a narrow local guild-entry detector.

Scene output never supplies actions, resource counts or completion facts.
The guild detector requires both measured glyph and label evidence on a
separately confirmed main menu. No external process or model service is used.
"""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import cv2
import numpy as np


def _feature(patch,support):
    gray=cv2.cvtColor(patch,cv2.COLOR_BGR2GRAY).astype(np.float32)
    vector=gray[support>0];vector-=vector.mean()
    return vector/max(float(np.linalg.norm(vector)),1e-12)


def _correlation(query,template,mask):
    """Pearson correlation over learned stable pixels, excluding wallpaper."""
    q=query[mask>0].astype(np.float32);t=template[mask>0].astype(np.float32)
    q-=q.mean();t-=t.mean();denom=float(np.linalg.norm(q)*np.linalg.norm(t))
    return float(q@t)/denom if denom>1e-6 else -1.


def _image(image):
    if image is None or image.ndim!=3 or image.shape[2]!=3:
        return None
    h,w=image.shape[:2]
    if w<640 or h<360 or abs(w/h-16/9)>.025:
        return None
    normalized=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA).copy()
    normalized[:80,:360]=0
    return normalized


class CommandVision:
    def __init__(self,root,threads=2):
        self.root=Path(root);self.folder=self.root/'models/gamevision_bd2'
        self.threads=threads;self.scene=None;self.glyph=None

    def identify(self,image):
        normalized=_image(image)
        if normalized is None:return {'available':False,'reason':'unsupported_client_geometry'}
        if self.scene is None:
            import onnxruntime as ort
            spec=json.loads((self.folder/'command_scene.json').read_text(encoding='utf-8'))
            path=self.folder/'command_scene.onnx'
            if hashlib.sha256(path.read_bytes()).hexdigest()!=spec['model_sha256']:
                raise ValueError('Trained scene model changed')
            if spec['input_size']!=[320,192] or spec['account_exclusion']!=[0,0,360,80]:
                raise ValueError('Trained scene preprocessing changed')
            options=ort.SessionOptions();options.intra_op_num_threads=self.threads
            options.inter_op_num_threads=1;options.log_severity_level=3
            options.execution_mode=ort.ExecutionMode.ORT_SEQUENTIAL
            self.scene=(spec,ort.InferenceSession(str(path),sess_options=options,providers=['CPUExecutionProvider']))
        spec,session=self.scene
        rgb=cv2.cvtColor(cv2.resize(normalized,(320,192),interpolation=cv2.INTER_AREA),cv2.COLOR_BGR2RGB)
        array=rgb.astype(np.float32)/255
        array=(array-np.array([.485,.456,.406],np.float32))/np.array([.229,.224,.225],np.float32)
        tensor=np.ascontiguousarray(array.transpose(2,0,1)[None])
        logits=session.run(None,{session.get_inputs()[0].name:tensor})[0][0]
        if len(logits)!=len(spec['classes']) or not np.isfinite(logits).all():
            raise ValueError('Invalid scene output')
        probs=np.exp(logits-logits.max());probs/=probs.sum();order=np.argsort(-probs)
        score=float(probs[order[0]]);margin=score-float(probs[order[1]])
        candidate=spec['classes'][int(order[0])]
        return dict(available=True,candidate_state=candidate,
            match=candidate if (candidate in spec.get('accepted_candidate_classes',[]) and
                score>=.985 and margin>=.35 and candidate!='transition') else None,
            score=round(score,5),margin=round(margin,5),model=spec['version'],
            score_type='uncalibrated_softmax_not_probability_of_success',
            account_region_excluded=True,action_authority=False)

    def guild_entry(self,image):
        normalized=_image(image)
        if normalized is None:return None
        if self.glyph is None:
            spec=json.loads((self.folder/'guild_anchor.json').read_text(encoding='utf-8'))
            path=self.folder/'guild_anchor.npz'
            if hashlib.sha256(path.read_bytes()).hexdigest()!=spec['sha256']:
                raise ValueError('Trained guild anchor changed')
            if spec['box']!=[202,82,280,145] or spec['point']!=[240,102]:
                raise ValueError('Unsupported guild anchor geometry')
            with np.load(path,allow_pickle=False) as arrays:
                weight=arrays['weight'].copy();bias=float(arrays['bias']);patterns=arrays['patterns'].copy();support=arrays['support'].copy()
            if patterns.ndim!=4 or patterns.shape[1:]!=(63,78,3):
                raise ValueError('Invalid guild anchor shapes')
            if not np.isfinite(weight).all() or not np.isfinite(bias):raise ValueError('Invalid guild weights')
            if spec['version']!=2 or support.shape!=(63,78) or support.dtype!=np.uint8 or cv2.countNonZero(support)<500:
                raise ValueError('Invalid guild foreground support')
            if cv2.countNonZero(support[:39,15:64])<200 or cv2.countNonZero(support[40:63,:])<200:
                raise ValueError('Insufficient separate glyph/label support')
            if weight.shape!=(cv2.countNonZero(support),) or spec['feature_size']!=len(weight):
                raise ValueError('Guild feature dimension mismatch')
            if (not np.isfinite(spec['threshold']) or spec['threshold']<=0 or
                spec['minimum_icon_correlation']!=.86 or spec['minimum_label_correlation']!=.90 or
                spec['minimum_ink_light']!=125):raise ValueError('Invalid guild model decision parameters')
            self.glyph=(spec,weight,bias,[cv2.cvtColor(x,cv2.COLOR_BGR2GRAY) for x in patterns],support)
        spec,weight,bias,patterns,support=self.glyph
        gray=cv2.cvtColor(normalized,cv2.COLOR_BGR2GRAY)
        best=None
        for dy in range(-3,4):
            for dx in range(-3,4):
                patch=normalized[82+dy:145+dy,202+dx:280+dx]
                logit=float(_feature(patch,support)@weight+bias)
                if not np.isfinite(logit) or logit<spec['threshold']:continue
                observed=gray[82+dy:145+dy,202+dx:280+dx]
                for reference in patterns:
                    scores=[]
                    for a,b,c,d in ((15,0,64,39),(0,40,78,63)):
                        query,template=observed[b:d,a:c],reference[b:d,a:c]
                        mask=support[b:d,a:c]
                        score=_correlation(query,template,mask)
                        ink=(template>=160)&(mask>0)
                        if not ink.any() or float(query[ink].mean())<spec['minimum_ink_light']:
                            score=-1
                        scores.append(score if np.isfinite(score) else -1)
                    if scores[0]<spec['minimum_icon_correlation'] or scores[1]<spec['minimum_label_correlation']:continue
                    quality=min(scores)
                    if best is None or quality>best['score']:
                        best=dict(score=round(quality,5),point=[240+dx,102+dy],
                            model='trained_local_guild_v2',logit=round(logit,5),
                            icon_score=round(scores[0],5),label_score=round(scores[1],5))
        return best


@lru_cache(maxsize=1)
def _runtime(root,threads):
    return CommandVision(root,threads)


def embedded_command_vision():
    from project_paths import ROOT
    from model_ocr_provider import _settings
    settings=_settings(ROOT)
    return _runtime(str(ROOT),settings['threads']) if settings else None


def scene_hint(image):
    try:
        runtime=embedded_command_vision()
        return runtime.identify(image) if runtime else {'available':False,'reason':'embedded_model_disabled'}
    except Exception as error:
        from execution_control import ExecutionControlError
        if isinstance(error,ExecutionControlError):raise
        return {'available':False,'reason':type(error).__name__}


def guild_entry_hint(image):
    try:
        runtime=embedded_command_vision()
        return runtime.guild_entry(image) if runtime else None
    except Exception as error:
        from execution_control import ExecutionControlError
        if isinstance(error,ExecutionControlError):raise
        return None
