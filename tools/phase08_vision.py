"""Read-only event, reward and guild recognition. No desktop input here."""
from frame_store import write_encoded_png
import json
import re
from pathlib import Path

import cv2

from daily_vision import DailyVision, OcrDocument, compact, windows_ocr
from m00_recognize import ROOT, read_image
from ui_foreground import foreground_mask, masked_shape_match


class Phase08Vision:
    def __init__(self):
        self.folder = ROOT / 'assets/phase08'
        self.specs = json.loads((self.folder / 'sources.json').read_text(encoding='utf-8'))
        self.templates = {name:read_image(self.folder / spec['file']) for name,spec in self.specs.items()}

    def anchor(self, image, name, pad=5, threshold=0.78, foreground=True, color_glyph=False):
        image = cv2.resize(image, (1280,720), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        template = cv2.cvtColor(self.templates[name], cv2.COLOR_BGR2GRAY)
        a,b,c,d = self.specs[name]['box']
        left,top,right,bottom = max(0,a-pad),max(0,b-pad),min(1280,c+pad),min(720,d+pad)
        if color_glyph:
            # White text remains less saturated than its animated golden glow.
            # Compare only this foreground, not a particular glow frame.
            mask=cv2.inRange(cv2.cvtColor(self.templates[name],cv2.COLOR_BGR2HSV),(0,0,170),(180,110,255))
            if cv2.countNonZero(mask)<12 or cv2.countNonZero(mask)>mask.size*0.9:return None
            region=cv2.inRange(cv2.cvtColor(image[top:bottom,left:right],cv2.COLOR_BGR2HSV),(0,0,170),(180,110,255))
            _, score, _, position=cv2.minMaxLoc(cv2.matchTemplate(region,mask,cv2.TM_CCOEFF_NORMED))
            match={'score':score,'position':position}
        elif foreground:
            match = masked_shape_match(gray[top:bottom,left:right], foreground_mask(template))
        else:
            _, score, _, position=cv2.minMaxLoc(cv2.matchTemplate(gray[top:bottom,left:right],template,cv2.TM_CCOEFF_NORMED))
            match={'score':score,'position':position}
        x,y = match['position']
        h,w = template.shape
        return {'score':float(match['score']), 'point':[left+x+w/2,top+y+h/2]} if match['score'] >= threshold else None

    @staticmethod
    def stage(doc, roi):
        candidates = []
        for line in doc.within(roi):
            match = re.search(r'(普通|挑战)战斗(\d+)', line['text'])
            if match:
                candidates.append({'mode':'normal' if match[1]=='普通' else 'challenge', 'stage':int(match[2])})
        return candidates[0] if len(candidates)==1 else None

    def inspect(self, image, payload):
        if abs(image.shape[1]/image.shape[0] - 16/9) > 0.025:
            return {'state':'unknown', 'reason':'unsupported_aspect_ratio'}
        doc = OcrDocument(payload)
        # Modal overlays take priority over the page visible underneath.
        guild = bool(self.anchor(image,'guild_title') and doc.find('公告事项',(130,235,300,290))
                     and doc.find('聊天',(500,60,600,115)))
        mailed = doc.find('奖励已发放至邮箱', (550,475,730,520))
        success = ((self.anchor(image,'guild_success',threshold=0.72)
                    or self.anchor(image,'guild_success_live',threshold=0.80,color_glyph=True)) if guild else None)
        if guild and mailed and success:
            return {'state':'guild_check_in_receipt','reward_destination':'mailbox',
                    'success_anchor':success,'points':{'dismiss':[760,498]}}
        if doc.find('点击画面即可返回',(400,595,900,690)):
            return {'state':'reward_overlay','points':{'dismiss':[768,645]}}
        if guild:
            return {'state':'guild_page','check_in_status':'unknown_without_receipt','points':{'back':[113,34]}}
        dialog = doc.find('自动战斗',(550,165,730,205))
        if dialog and doc.find('仅使用免费活动AP',(400,215,650,255)):
            return {'state':'event_auto_dialog', **(self.stage(doc,(550,195,740,225)) or {}),
                    'total_cost':doc.extract_int(r'^战斗(\d+)$',(590,530,795,580)),
                    'free_only':DailyVision.toggle(image,(815,237,852,258)),
                    'points':{'min':[500,312],'plus':[815,357],'minus':[465,357],
                              'submit':[695,554],'cancel':[537,554],'free_toggle':[829,248]}}
        victory = self.anchor(image,'victory',threshold=0.76)
        if victory:
            stage = self.stage(doc,(930,105,1180,155))
            if stage:
                return {'state':'event_victory',**stage,'victory_anchor':victory,
                        'auto_progress':self.progress(doc),'points':{'return':[965,674]}}
        progress = self.progress(doc)
        if progress:
            return {'state':'event_auto_running','auto_progress':progress}
        if doc.find('通关奖励',(470,140,700,200)) and doc.find('反复奖励',(1050,65,1190,110)):
            stage = self.stage(doc,(570,75,780,110))
            if stage:
                return {'state':'event_preparation',**stage,
                        'unit_cost':doc.extract_int(r'^战斗(\d+)$',(650,635,950,690)),
                        'points':{'open_auto':[1140,655]}}
        if self.anchor(image,'pass_title'):
            tasks = bool(doc.find('通行证经验',(600,225,1030,490))) or sum(
                bool(doc.find(text,(600,190,1030,490))) for text in ('餐厅','女神像','每日登录'))>=2
            levels = bool(doc.find('基础',(700,190,800,225)) and doc.find('高级',(875,190,990,225)))
            return {'state':'pass_tasks' if tasks else 'pass_levels' if levels else 'pass_page_unknown_tab',
                    'claim_status':'unknown', 'points':{'claim_all':[950,511],
                    'tasks_tab':[1063,452],'levels_tab':[1063,267]}}
        title_lines = doc.within((140,10,300,55))
        title = ''.join(line['text'] for line in title_lines)
        if '每日任务' in title or '每周任务' in title:
            kind = 'weekly' if '每周任务' in title else 'daily'
            rows = []
            for line in doc.within((450,90,850,640)):
                match = re.search(r'(.+?)(\d+)/(\d+)$',line['text'])
                if match:
                    rows.append({'name':match[1],'current':int(match[2]),'target':int(match[3])})
            return {'state':kind+'_tasks','rows':rows,
                    'incomplete_visible':[row for row in rows if row['current'] < row['target']],
                    'claim_status':'unknown','milestone_status':'unknown',
                    'points':{'claim_all':[1120,673],'daily_tab':[190,100],'weekly_tab':[190,147]}}
        # Reuse the wallpaper-independent home recognizer before exposing its icon.
        from common_navigation import PageDiagnosis
        if PageDiagnosis().inspect(image)['state']=='main_menu':
            entry = self.anchor(image,'guild_entry',threshold=0.80)
            if entry is None:
                # The menu has already been independently confirmed. This narrow
                # fallback checks a learned glyph AND its label; it never marks
                # check-in completed. The worker re-captures before committing.
                from command_vision import guild_entry_hint
                entry = guild_entry_hint(image)
            return {'state':'main_menu','guild_entry':entry}
        return {'state':'unknown'}

    @staticmethod
    def progress(doc):
        candidates=[]
        for line in doc.within((30,610,470,675)):
            match=re.search(r'第(\d+)次/共(\d+)次',line['text'])
            if match:
                candidates.append({'current':int(match[1]),'total':int(match[2])})
        return candidates[0] if len(candidates)==1 else None

    def inspect_file(self,path,output,payload=None):
        path, output = Path(path).resolve(), Path(output).resolve()
        payload = payload if payload is not None else windows_ocr(path,output/'ocr')
        image = read_image(path)
        result = self.inspect(image,payload)
        # Numeric UI is often missed by full-frame Chinese OCR. Keep crops and
        # obtain two agreeing English OCR readings; never infer a digit on failure.
        boxes = {'free_ap':(1163,30,1194,47)} if result['state'].startswith('event_') else {}
        if result['state']=='event_auto_dialog' and result['total_cost'] is None:
            boxes['total_cost']=(712,537,727,557)
        if result['state']=='event_preparation' and result['unit_cost'] is None:
            boxes['unit_cost']=(895,652,906,668)
        normalized = cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
        output.mkdir(parents=True,exist_ok=True)
        for key,(a,b,c,d) in boxes.items():
            readings=[]
            for scale in (3,4):
                gray=cv2.cvtColor(normalized[b:d,a:c],cv2.COLOR_BGR2GRAY)
                _,crop=cv2.threshold(gray,0,255,cv2.THRESH_BINARY|cv2.THRESH_OTSU)
                crop=cv2.copyMakeBorder(crop,10,10,10,10,cv2.BORDER_CONSTANT,value=0)
                crop=cv2.resize(crop,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC)
                target=output/f'{path.stem}_{key}_{scale}.png'
                write_encoded_png(target, cv2.imencode('.png',crop)[1])
                data=windows_ocr(target,output/'ocr',language='en-US')
                value=compact(''.join(line['text'] for line in data['lines']))
                pattern = r'^(\d+)/\d+$' if key=='free_ap' else r'^(\d+)$'
                match=re.fullmatch(pattern,value)
                readings.append(int(match[1]) if match else None)
            result[key+'_readings']=readings
            if readings[0] is not None and readings[0]==readings[1]:
                result[key]=readings[0]
                result[key+'_source']='same_frame_OCR_agreement'
            else:
                # Windows OCR ignores these small isolated glyphs in the supplied
                # recordings. Narrow fallback only for observed values/styles.
                # Unknown numbers are not rounded to the nearest known value.
                best_by_value={}
                region=cv2.cvtColor(normalized[b:d,a:c],cv2.COLOR_BGR2GRAY)
                for name,spec in self.specs.items():
                    if spec.get('numeric_kind')!=key:
                        continue
                    patch=cv2.cvtColor(self.templates[name],cv2.COLOR_BGR2GRAY)
                    if patch.shape != region.shape or float(region.std())<8:
                        continue
                    score=float(cv2.matchTemplate(region,patch,cv2.TM_CCOEFF_NORMED)[0,0])
                    value=spec['value']
                    best_by_value[value]=max(score,best_by_value.get(value,-1.0))
                candidates=sorted(((score,value) for value,score in best_by_value.items()),reverse=True)
                result[key+'_template_scores']=[{'score':score,'value':value} for score,value in candidates]
                if candidates and candidates[0][0]>=0.97 and (len(candidates)==1 or candidates[0][0]-candidates[1][0]>=0.06):
                    result[key]=candidates[0][1]
                    result[key+'_source']='observed_UI_numeric_template_restricted_values'
        return result
