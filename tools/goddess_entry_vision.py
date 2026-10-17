"""Recognize the demonstrated card collection route without game input."""
import json
import cv2
from project_paths import ROOT
from m00_recognize import read_image
from ui_foreground import foreground_mask, masked_shape_match


class GoddessEntryVision:
    def __init__(self):
        assets=ROOT/'assets/goddess_entry'
        self.specs=json.loads((assets/'sources.json').read_text(encoding='utf-8')) if (assets/'sources.json').exists() else {}
        self.images={name:read_image(assets/spec['file']) for name,spec in self.specs.items()}

    def match(self,image,name,roi=None,foreground=True,threshold=.76):
        if name not in self.images: return None
        normalized=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
        gray=cv2.cvtColor(normalized,cv2.COLOR_BGR2GRAY)
        template=cv2.cvtColor(self.images[name],cv2.COLOR_BGR2GRAY)
        a,b,c,d=roi or self.specs[name]['box']
        if roi is None: a,b,c,d=max(0,a-10),max(0,b-10),min(1280,c+10),min(720,d+10)
        region=gray[b:d,a:c]
        best=None
        for factor in (.96,1.0,1.04):
            patch=cv2.resize(template,None,fx=factor,fy=factor,interpolation=cv2.INTER_AREA)
            if foreground:
                hit=masked_shape_match(region,foreground_mask(patch))
                score,(x,y)=hit['score'],hit['position']
            else:
                _,score,_,(x,y)=cv2.minMaxLoc(cv2.matchTemplate(region,patch,cv2.TM_CCOEFF_NORMED))
            if best is None or score>best['score']:
                best={'score':float(score),'point':[a+x+patch.shape[1]/2,b+y+patch.shape[0]/2]}
        return best if best and best['score']>=threshold else None

    def collection_point(self,image,doc=None):
        result=self.match(image,'home_collection',threshold=.70)
        key=self.match(image,'home_cartridge_key',threshold=.70)
        if result and key:
            spec=self.specs['home_collection']
            a,b,c,d=spec.get('click_box',spec['box'])
            point=spec.get('click_point',[(a+c)/2,(b+d)/2])
            return point if a<=point[0]<=c and b<=point[1]<=d else None
        return None

    def library(self,image,doc=None):
        if not self.match(image,'library_title',threshold=.82): return None
        label=doc.find('奇幻广场',(80,90,1220,700)) if doc else None
        logo=self.match(image,'square_logo',roi=(75,65,1230,690),foreground=False,threshold=.82)
        if not logo:
            logo=self.match(image,'square_logo_active',roi=(75,65,1230,690),foreground=False,threshold=.82)
        if label:
            point=[label['point'][0],label['point'][1]-70]
        else:
            point=logo['point'] if logo else None
        return {'state':'goddess_collection','square_entry_point':point,
                'back_point':[113,33],'target':'奇幻广场',
                'evidence':'caption' if label else 'logo' if logo else 'target_not_visible'}
