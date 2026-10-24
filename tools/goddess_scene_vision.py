"""[广场图形] 女神像翼形与羽纹、左下方向键和下中四钮共同确认场景；独立房子/H才授权退出。"""
import json
import cv2
import numpy as np
from project_paths import ROOT
from m00_recognize import read_image


class GoddessSceneVision:
    def __init__(self):
        folder=ROOT/'assets/goddess_scene'
        self.spec=json.loads((folder/'sources.json').read_text(encoding='utf-8'))
        self.images={key:read_image(folder/item['file']) for key,item in self.spec['anchors'].items()}
        self.masks={key:cv2.cvtColor(read_image(folder/item['mask_file']),cv2.COLOR_BGR2GRAY)
                    for key,item in self.spec['anchors'].items() if item.get('mask_file')}

    def match(self,normalized,key):
        """[局部核实] 像体可小范围平移，界面只搜固定邻域；白色前景必须保持明亮。"""
        item=self.spec['anchors'][key];a,b,c,d=item['box'];padding=item['padding']
        x1,y1=max(0,a-padding),max(0,b-padding)
        region=cv2.cvtColor(normalized[y1:min(720,d+padding),x1:min(1280,c+padding)],cv2.COLOR_BGR2GRAY)
        ref=cv2.cvtColor(self.images[key],cv2.COLOR_BGR2GRAY)
        foreground=np.uint8(ref>=item.get('foreground_gray',210))*255
        if key in self.masks:
            # [房子/H] 只在显式白轮廓及一像素边界上比较灰度，保留抗锯齿；徽章整块剔除。
            foreground=self.masks[key]
            region_values=region
        else:region_values=region
        support=cv2.dilate(foreground,np.ones((3,3),np.uint8))
        if item.get('ignored_badge_box'):
            ia,ib,ic,id=item['ignored_badge_box'];support[ib:id,ia:ic]=0
        values=cv2.matchTemplate(region_values,ref,cv2.TM_CCOEFF_NORMED,
            **({'mask':support} if item.get('ui') else {}))
        values[~np.isfinite(values)]=-1
        _,score,_,(x,y)=cv2.minMaxLoc(values)
        sample=region[y:y+ref.shape[0],x:x+ref.shape[1]]
        brightness=float(sample[foreground>0].mean()) if foreground.any() else 0
        if score<item['threshold'] or brightness<item['min_brightness']:return None
        return {'score':round(score,4),'brightness':round(brightness,1),
            'point':[x1+x+ref.shape[1]/2,y1+y+ref.shape[0]/2],
            'offset':[x1+x-a,y1+y-b]}

    def inspect(self,image):
        """[三标志] 缺一类即不认场景；不读创建队伍、旧许愿提示或人物站位。"""
        h,w=image.shape[:2]
        if abs(w/h-16/9)>.025:return None
        normalized=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA);groups={}
        for group,keys in self.spec['groups'].items():
            found={key:self.match(normalized,key) for key in keys}
            if not all(found.values()):return None
            if group=='statue':
                offsets=[value['offset'] for value in found.values()]
                if max(abs(offsets[0][i]-offsets[1][i]) for i in (0,1))>8:return None
            groups[group]=found
        return {'state':'goddess_scene','rule':'statue_wing_feathers_and_dpad_and_four_bottom_buttons','groups':groups}

    def home(self,image,scene=None):
        """[退出合同] 场景三标志之外，房子和H键分别匹配且上下对齐才给点位。"""
        scene=scene or self.inspect(image)
        if not scene:return None
        h,w=image.shape[:2];normalized=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
        house=self.match(normalized,'home_house');key=self.match(normalized,'home_key')
        if not house or not key:return None
        dx,dy=[key['point'][i]-house['point'][i] for i in (0,1)]
        if abs(dx)>7 or not 12<=dy<=35:return None
        return {'variant':'goddess_three_markers_house_H','score':min(house['score'],key['score']),
            'client_point':[house['point'][0]*w/1280,house['point'][1]*h/720],
            'pair_evidence':{'house':house,'key':key},'scene_evidence':scene}
