"""Mirror cartridge and lobby portal recognition, without game input."""
import json
import cv2
import numpy as np
from goddess_entry_vision import GoddessEntryVision
from m00_recognize import ROOT,read_image


class MirrorEntryVision(GoddessEntryVision):
    def __init__(self):
        assets=ROOT/'assets/mirror_entry'
        path=assets/'sources.json'
        self.specs=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        self.images={name:read_image(assets/spec['file']) for name,spec in self.specs.items()}

    def card_point(self,image,doc=None):
        caption=doc.find('镜中之战',(80,90,1220,700)) if doc else None
        if caption:
            return [caption['point'][0],caption['point'][1]-70]
        logo=self.match(image,'card_logo',roi=(75,65,1230,690),foreground=False,threshold=.80)
        return logo['point'] if logo else None

    def lobby(self,image):
        if not (self.match(image,'lobby_score',threshold=.70)
                and self.match(image,'lobby_wine',foreground=False,threshold=.78)):
            return None
        normalized=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
        hsv=cv2.cvtColor(normalized,cv2.COLOR_BGR2HSV)
        red=cv2.inRange(hsv,np.array([0,100,45]),np.array([12,255,255]))
        red|=cv2.inRange(hsv,np.array([170,100,45]),np.array([180,255,255]))
        bounded=np.zeros_like(red);bounded[205:585,245:950]=red[205:585,245:950]
        bounded=cv2.morphologyEx(bounded,cv2.MORPH_CLOSE,np.ones((9,9),np.uint8))
        contours,_=cv2.findContours(bounded,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        candidates=[]
        for contour in contours:
            area=cv2.contourArea(contour);x,y,w,h=cv2.boundingRect(contour)
            if 1500<=area<=50000 and 90<=w<=350 and 45<=h<=220 and 1.25<=w/h<=4 and area/(w*h)>=.30:
                candidates.append({'point':[x+w/2,y+h/2],'area':area,'box':[x,y,w,h]})
        candidates.sort(key=lambda c:c['area'],reverse=True)
        chosen=candidates[0] if candidates and (len(candidates)==1 or candidates[0]['area']>=candidates[1]['area']*1.5) else None
        return {'state':'mirror_lobby','arena_entry_point':chosen['point'] if chosen else None,
                'portal_candidates':candidates,'evidence':'mirror_score_and_wine_UI_with_red_platform'}

    def multiplier_dialog(self,image,doc=None):
        if not self.match(image,'multiplier_title',threshold=.78):
            return None
        value=doc.extract_int(r'^[^\d]*(\d{1,2})倍$',(590,365,690,425)) if doc else None
        return {'state':'mirror_multiplier_dialog','multiplier':value if value is not None and 1<=value<=40 else None,
                'points':{'min':[491,357],'minus5':[591,357],'plus5':[692,357],
                          'minus':[452,425],'plus':[829,425],'confirm':[694,504],'cancel':[537,504]}}

    def fixed_number(self,image,field):
        """Recognize exact demonstrated labels, independently of the other numeric fields."""
        normalized=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
        variants={'smooth':[],'binary':[]}
        for name,spec in self.specs.items():
            if spec.get('field')!=field:continue
            a,b,c,d=spec['box'];region=normalized[b-2:d+2,a-2:c+2]
            gray=cv2.cvtColor(region,cv2.COLOR_BGR2GRAY)
            template=cv2.cvtColor(self.images[name],cv2.COLOR_BGR2GRAY)
            if float(gray.max())-float(gray.min())<20:continue
            for variant in variants:
                if variant=='smooth':
                    region_value=cv2.GaussianBlur(gray,(3,3),.6)
                    template_value=cv2.GaussianBlur(template,(3,3),.6)
                else:
                    _,region_value=cv2.threshold(gray,0,255,cv2.THRESH_BINARY|cv2.THRESH_OTSU)
                    _,template_value=cv2.threshold(template,0,255,cv2.THRESH_BINARY|cv2.THRESH_OTSU)
                score=float(cv2.matchTemplate(region_value,template_value,cv2.TM_CCOEFF_NORMED).max())
                variants[variant].append((score,spec['value']))
        accepted=[]
        for variant,scores in variants.items():
            scores.sort(reverse=True)
            if len(scores)>=2 and scores[0][0]>=.94 and scores[0][0]-scores[1][0]>=.05:
                accepted.append((scores[0][0],scores[0][1],variant))
        if accepted and len({item[1] for item in accepted})==1:
            score,value,variant=max(accepted)
            return {'value':value,'score':round(score,4),'source':'demonstrated_'+field+'_label','variant':variant}
        return None

    def price_box(self,image):
        image=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
        hsv=cv2.cvtColor(image[522:558,650:790],cv2.COLOR_BGR2HSV)
        red=cv2.inRange(hsv,np.array([0,85,45]),np.array([12,255,255]))
        red|=cv2.inRange(hsv,np.array([170,85,45]),np.array([180,255,255]))
        contours,_=cv2.findContours(red,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        boxes=[cv2.boundingRect(c) for c in contours if cv2.contourArea(c)>=3]
        boxes=[(x+650,y+522,w,h) for x,y,w,h in boxes if 2<=w<=20 and 2<=h<=22]
        if len(boxes)!=1:return None
        x,y,w,h=boxes[0]
        return [x+w+2,529,min(x+w+36,810),553]

    def slider_multiplier(self,image):
        image=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
        patch=image[417:434,475:811]
        hsv=cv2.cvtColor(image[407:455,475:811],cv2.COLOR_BGR2HSV)
        # A mascot/click effect covering the track makes its endpoint uncertain.
        if int((hsv[:,:,1]>65).sum())>18:return None
        gray=cv2.cvtColor(patch,cv2.COLOR_BGR2GRAY)
        ref=cv2.cvtColor(image[497:510,720:765],cv2.COLOR_BGR2GRAY)
        cutoff=max(90,float(np.median(ref))*.80)
        selected=(gray>=cutoff).sum(axis=0)>=7
        xs=np.flatnonzero(selected)
        if not len(xs) or not 475<=int(xs[0])+475<=483:return None
        last=int(xs[0]);gaps=0
        for x in range(last,len(selected)):
            if selected[x]:last=x;gaps=0
            else:gaps+=1
            if gaps>=4:break
        endpoint=last+475
        value=1+(endpoint-503)*39/298
        chosen=round(value)
        if 1<=chosen<=40 and abs(endpoint-(503+(chosen-1)*298/39))<=2.4:
            return {'value':chosen,'endpoint':endpoint,'source':'unoccluded_slider_position'}
        return None
