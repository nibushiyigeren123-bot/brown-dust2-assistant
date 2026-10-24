"""Return controls only. No OCR, account data, window control or module imports."""
import json
import cv2
import numpy as np
from project_paths import ROOT
from ui_foreground import foreground_mask, masked_shape_match


class ReturnNavigationVision:
    def __init__(self):
        folder=ROOT/'assets/return_navigation'
        self.specs=json.loads((folder/'sources.json').read_text(encoding='utf-8'))
        self.images={k:cv2.imdecode(np.fromfile(str(folder/v['file']),dtype=np.uint8),cv2.IMREAD_COLOR)
                     for k,v in self.specs.items()}
        self.arrow=foreground_mask(self.images['white_back'])
        # [退出合同] 从可追溯小图配置读取页面身份与退出控件，永不读取业务数量或消费设置。
        self.contracts=[spec['navigation_contract'] for spec in self.specs.values()
                        if 'navigation_contract' in spec]

    def inspect(self, image):
        h,w=image.shape[:2]
        if abs(w/h-16/9)>.025:return None
        norm=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
        gray=cv2.cvtColor(norm,cv2.COLOR_BGR2GRAY)
        def score(key):
            a,b,c,d=self.specs[key]['box']
            patch=norm[b:d,a:c]
            return float(cv2.matchTemplate(patch,self.images[key],cv2.TM_CCOEFF_NORMED)[0,0])
        def visible(key, minimum=.8):
            a,b,c,d=self.specs[key]['box']
            reference=cv2.cvtColor(self.images[key],cv2.COLOR_BGR2GRAY)
            mask=reference>=max(1,np.percentile(reference,70))
            if not mask.any():return False
            return float(gray[b:d,a:c][mask].mean())>=minimum*float(reference[mask].mean())
        def control(state, action, point, scores):
            return {'state':state,'next_action':action,'return_button':{
                'client_point':[point[0]*w/1280,point[1]*h/720],
                'normalized_point':point,'scores':scores}}
        # [独立复核] 标题/身份和退出按钮必须同时清晰可见；暗遮罩下的背景按钮不能授权。
        for spec in self.contracts:
            scores={key:round(score(key),4) for key in spec['keys']}
            if all(np.isfinite(value) and value>=spec.get('threshold',.94) for value in scores.values()) and all(visible(key,.85) for key in spec['keys']):
                # [通行证退出] 双锚只证明页面；旧合同空白点不能退出，必须独立找到明亮的顶部箭头。
                # 仅修四种普通通行证合同，复用原white_back图形、顶部ROI和可见阈值；其它合同原样处理。
                if spec['state'] in ('pass_tasks_special', 'pass_levels_special',
                                     'pass_tasks_season', 'pass_levels_season'):
                    match=masked_shape_match(gray[13:55,95:135],self.arrow)
                    if match['score']<.82 or match['foreground_gray']<175:
                        return None
                    px,py=match['position'];hh,ww=self.arrow.shape
                    return control(spec['state'],'click_back',[95+px+ww/2,13+py+hh/2],
                        {**scores,'back_shape':round(match['score'],4),
                         'back_brightness':round(match['foreground_gray'],1)})
                return control(spec['state'],spec['action'],spec['point'],scores)
        # [暗页边界] 双锚未成立的黑屏/暗未知页仍按原阈值拒绝，不放行通用返回猜测。
        if float((gray<35).mean())>.88:return None
        panels=[
            ('event_quick_dialog','cancel_panel',[536,541],('event_quick_title','event_quick_cancel')),
            ('event_auto_dialog','cancel_panel',[536,549],('event_auto_title','event_auto_cancel')),
            ('management_panel','cancel_panel',[554,555],('management_title','management_cancel')),
            ('talk_panel','close_panel',[998,182],('talk_title','talk_close')),
            ('equipment_filter','close_panel',[973,160],('equipment_filter_title','equipment_filter_close')),
            ('equipment_detail','close_panel',[946,217],('equipment_detail_marker','equipment_detail_refine','equipment_detail_close')),
            ('hunt_dialog','cancel_panel',[536,522],('hunt_title','hunt_cancel')),
        ]
        for state,action,point,keys in panels:
            scores={key:round(score(key),4) for key in keys}
            if all(value>=.94 for value in scores.values()) and all(visible(key) for key in keys):
                return control(state,action,point,scores)
        for key in ('circle_back','library_back'):
            circle=score(key)
            if circle>=.94 and visible(key,.85) and gray[10:54,94:136].mean()>100:
                return control('page_with_circle_back','click_back',[115,32],{'circle':round(circle,4)})
        narrow=score('narrow_back')
        if narrow>=.94 and visible('narrow_back',.85):
            return control('page_with_narrow_back','click_back',[76,33],{'narrow':round(narrow,4)})
        for left,right in ((95,135),(55,95)):
            match=masked_shape_match(gray[13:55,left:right],self.arrow)
            # A dimmed arrow behind a modal/loading overlay is not an exit control.
            if match['score']>=.82 and match['foreground_gray']>=175:
                px,py=match['position'];hh,ww=self.arrow.shape
                return control('page_with_back','click_back',[left+px+ww/2,13+py+hh/2],
                               {'shape':round(match['score'],4),'brightness':round(match['foreground_gray'],1)})
        return None
