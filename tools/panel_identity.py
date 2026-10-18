"""Small local tab/identity templates distinguish pass panels from opening ads."""
import json
import cv2
import numpy as np
from project_paths import ROOT


class PassPanelSignature:
    def __init__(self):
        folder=ROOT/'assets/pass_rewards'
        specs=json.loads((folder/'sources.json').read_text(encoding='utf-8'))
        self.items=[(key,spec,cv2.imdecode(np.fromfile(str(folder/spec['file']),dtype=np.uint8),cv2.IMREAD_COLOR)) for key,spec in specs.items()]

    def identify(self,normalized):
        scores={}
        for key,spec,template in self.items:
            a,b,c,d=spec['box']
            scores[key]=float(cv2.matchTemplate(normalized[b:d,a:c],template,cv2.TM_CCOEFF_NORMED)[0,0])
        def best(prefix):
            ranked=sorted((key for key in scores if key.startswith(prefix)),key=scores.get,reverse=True)
            if len(ranked)<2 or scores[ranked[0]]<.97 or scores[ranked[0]]-scores[ranked[1]]<.008:return None
            return ranked[0].split(':')[-1]
        identity=best('identity:')
        tab=best('tab:'+identity+':') if identity else None
        return {'pass_id':identity,'tab':tab} if identity and tab else None
