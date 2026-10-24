"""Local equipment UI recognition. Does not send game input."""
from frame_store import write_encoded_png
import hashlib
import json
import re
from pathlib import Path

import cv2

from daily_vision import OcrDocument, compact, windows_ocr
from m00_recognize import ROOT, read_image


def read_inventory_count(image, output):
    image = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)
    # Include the entire numerator: the previous ROI clipped the leading 2
    # from 272. Inventory is auxiliary; never reconstruct missing digits.
    crop = cv2.cvtColor(image[23:50, 231:320], cv2.COLOR_BGR2GRAY)
    crop = cv2.copyMakeBorder(crop, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=0)
    readings, values = [], []
    for scale in (3, 4):
        encoded = cv2.imencode('.png', cv2.resize(crop, None, fx=scale, fy=scale,
                                                interpolation=cv2.INTER_CUBIC))[1]
        path = Path(output) / ('inventory_' + hashlib.sha256(encoded.tobytes()).hexdigest()[:16] + '.png')
        path.parent.mkdir(parents=True, exist_ok=True)
        write_encoded_png(path, encoded)
        payload = windows_ocr(path, Path(output) / 'ocr', language='en-US')
        text = compact(''.join(line['text'] for line in payload['lines']))
        readings.append(text)
        match = re.fullmatch(r'(\d{1,4})/2000', text)
        values.append(int(match[1]) if match and 0 <= int(match[1]) <= 2000 else None)
    return {'value': values[0] if values[0] is not None and values[0] == values[1] else None,
            'readings': readings}


def read_integer(image, box, output, label):
    """Require agreeing complete readings; preserve native pixels for fallback."""
    native_image = image
    image = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)
    a, b, c, d = box
    gray = cv2.cvtColor(image[b:d, a:c], cv2.COLOR_BGR2GRAY)
    gray = cv2.copyMakeBorder(gray, 6, 6, 6, 6, cv2.BORDER_CONSTANT, value=0)
    values, readings = [], []
    for scale in (3, 4):
        encoded = cv2.imencode('.png', cv2.resize(gray, None, fx=scale, fy=scale,
                                                interpolation=cv2.INTER_CUBIC))[1]
        path = Path(output) / (label + '_' + hashlib.sha256(encoded.tobytes()).hexdigest()[:16] + '.png')
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            write_encoded_png(path, encoded)
        payload = windows_ocr(path, Path(output) / 'ocr', language='en-US')
        text = compact(''.join(line['text'] for line in payload['lines']))
        readings.append(text)
        valid = re.fullmatch(r'(?:\d{1,3}(?:[,.]\d{3})+|\d+)', text)
        values.append(int(text.replace(',', '').replace('.', '')) if valid else None)
    value = values[0] if values[0] is not None and values[0] == values[1] else None
    if value is None:
        # WinRT OCR may ignore an isolated number. Require and strip a known
        # context prefix; still accept only exact integers agreeing at two sizes.
        field = cv2.cvtColor(image[b:d, a:c], cv2.COLOR_BGR2GRAY)
        contextual = cv2.copyMakeBorder(field, 12, 12, 140, 12, cv2.BORDER_CONSTANT, value=0)
        cv2.putText(contextual, 'MATERIAL', (4, 32),
                    cv2.FONT_HERSHEY_SIMPLEX, .6, 255, 1, cv2.LINE_AA)
        fallback_values = []
        for scale in (3, 5):
            encoded = cv2.imencode('.png', cv2.resize(contextual, None, fx=scale, fy=scale,
                                                    interpolation=cv2.INTER_CUBIC))[1]
            path = Path(output) / (label + '_context_' + hashlib.sha256(encoded.tobytes()).hexdigest()[:16] + '.png')
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                write_encoded_png(path, encoded)
            payload = windows_ocr(path, Path(output) / 'ocr', language='en-US')
            text = compact(''.join(line['text'] for line in payload['lines']))
            readings.append('context:' + text)
            text = text[len('MATERIAL'):] if text.startswith('MATERIAL') else ''
            valid = re.fullmatch(r'(?:\d{1,3}(?:[,.]\d{3})+|\d+)', text)
            fallback_values.append(int(text.replace(',', '').replace('.', '')) if valid else None)
        value = fallback_values[0] if fallback_values[0] is not None and fallback_values[0] == fallback_values[1] else None
    if value is None:
        native = read_native_integer(native_image, box, output, label)
        readings.extend('native:' + item for item in native['readings'])
        value = native['value']
    return {'value': value, 'readings': readings}


def read_native_integer(image, box, output, label):
    height,width=image.shape[:2]
    a,b,c,d=box
    field=cv2.cvtColor(image[round(b*height/720):round(d*height/720),
                            round(a*width/1280):round(c*width/1280)],cv2.COLOR_BGR2GRAY)
    field=cv2.copyMakeBorder(field,12,12,12,12,cv2.BORDER_CONSTANT,value=0)
    readings,values=[],[]
    for scale in (2,3,4):
        encoded=cv2.imencode('.png',cv2.resize(field,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC))[1]
        path=Path(output)/(label+'_native_'+hashlib.sha256(encoded.tobytes()).hexdigest()[:16]+'.png')
        path.parent.mkdir(parents=True,exist_ok=True)
        if not path.exists():write_encoded_png(path, encoded)
        payload=windows_ocr(path,Path(output)/'ocr',language='en-US')
        text=compact(''.join(line['text'] for line in payload['lines']))
        readings.append(text)
        if re.fullmatch(r'(?:\d{1,3}(?:[,.]\d{3})+|\d+)',text):
            values.append(int(text.replace(',','').replace('.','')))
    value=values[0] if len(values)>=2 and len(set(values))==1 else None
    return {'value':value,'readings':readings}


def read_plain_badge(image, box, output, label='badge'):
    """[功能] 完整徽章两尺度读取，只接受1..23纯数字；加号、24和不一致均未知。"""
    normalized=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
    a,b,c,d=map(int,box)
    patch=normalized[b:d,a:c]
    readings=[]
    for scale in (3,4):
        crop=cv2.copyMakeBorder(patch,5,5,5,5,cv2.BORDER_CONSTANT,value=(0,0,0))
        raw=cv2.imencode('.png',cv2.resize(crop,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC))[1]
        path=Path(output)/(label+'_'+hashlib.sha256(raw.tobytes()).hexdigest()[:20]+'.png')
        path.parent.mkdir(parents=True,exist_ok=True)
        if not path.exists():write_encoded_png(path,raw)
        payload=windows_ocr(path,Path(output)/'ocr',language='en-US')
        readings.append(compact(''.join(line['text'] for line in payload['lines'])))
    # [图形门禁] 先核对完整菱形边框，中央数字遮罩后比较，物品纹理即使OCR出2也不能成为徽章。
    specs=json.loads((ROOT/'assets/equipment/sources.json').read_text(encoding='utf-8'))
    shape_scores=[]
    if label.startswith(('grid','badge_','detail','refine')):
        import numpy as np
        gray=cv2.cvtColor(patch,cv2.COLOR_BGR2GRAY)
        for name,spec in specs.items():
            family='detail_score' if label.startswith('detail') else 'refine_score' if label.startswith('refine') else 'badge_'
            if not name.startswith(family) or not name.removeprefix(family).isdigit():continue
            template=read_image(ROOT/'assets/equipment'/spec['file']);t=cv2.cvtColor(template,cv2.COLOR_BGR2GRAY)
            if gray.shape[0]<t.shape[0] or gray.shape[1]<t.shape[1]:continue
            mask=np.ones(t.shape,np.uint8)*255
            if label.startswith('detail'):mask[7:29,6:25]=0
            elif label.startswith('refine'):mask[10:39,9:35]=0
            else:mask[5:23,6:24]=0
            score=float(np.nanmax(cv2.matchTemplate(gray,t,cv2.TM_CCOEFF_NORMED,mask=mask)))
            shape_scores.append(score)
        if not shape_scores or max(shape_scores)<.75:
            return {'value':None,'readings':readings,'box':list(box),'badge_shape_verified':False,'shape_score':max(shape_scores,default=-1.)}
        # 完整区域发现加号直接拒绝；仅在菱形存在时读内部完整两位数字，避免边框被OCR当尖括号。
        if any('+' in text or '＋' in text for text in readings):
            return {'value':None,'readings':readings,'box':list(box),'badge_shape_verified':True,'reason':'plus_badge'}
        if patch.shape[:2] in ((35,36),(34,31),(47,44)):
            inner=patch[9:29,8:28] if patch.shape[:2]==(35,36) else patch[9:29,7:24] if patch.shape[:2]==(34,31) else patch[13:37,12:32]
            numeric=[]
            for scale in (1,2):
                crop=cv2.copyMakeBorder(cv2.resize(inner,None,fx=scale,fy=scale),4,4,4,4,cv2.BORDER_CONSTANT,value=(0,0,0))
                raw=cv2.imencode('.png',crop)[1];path=Path(output)/('inner_'+hashlib.sha256(raw.tobytes()).hexdigest()[:20]+'.png')
                if not path.exists():write_encoded_png(path,raw)
                payload=windows_ocr(path,Path(output)/'ocr',language='en-US')
                numeric.append(compact(''.join(line['text'] for line in payload['lines'])))
            readings.extend('inner:'+text for text in numeric)
            valid_numeric=[int(text) if re.fullmatch(r'\d{1,2}',text) and 1<=int(text)<24 else None for text in numeric]
            if numeric[0]==numeric[1] and re.fullmatch(r'\d{1,2}',numeric[0]) and not 1<=int(numeric[0])<24:
                return {'value':None,'readings':readings,'box':list(box),'badge_shape_verified':True,'shape_score':max(shape_scores),'reason':'outside_1_to_23'}
            if valid_numeric[0] is not None and valid_numeric[0]==valid_numeric[1]:
                return {'value':valid_numeric[0],'readings':readings,'box':list(box),'badge_shape_verified':True,'shape_score':max(shape_scores),'policy':'diamond_then_complete_inner_digits_two_scale'}
    valid=[int(text) if re.fullmatch(r'\d{1,2}',text) and 1<=int(text)<24 else None for text in readings]
    value=valid[0] if valid[0] is not None and valid[0]==valid[1] else None
    # [兼容/证据] 完整旧徽章裁片可独立证明数字；它包含边框，不能只从19+中截出19。
    matches=[]
    specs=json.loads((ROOT/'assets/equipment/sources.json').read_text(encoding='utf-8'))
    family='detail_score' if label.startswith('detail') else 'refine_score' if label.startswith('refine') else 'badge_'
    for name,spec in specs.items():
        if not name.startswith(family) or not name.removeprefix(family).isdigit():continue
        number=int(name.removeprefix(family))
        template=read_image(ROOT/'assets/equipment'/spec['file'])
        if patch.shape[0]<template.shape[0] or patch.shape[1]<template.shape[1]:continue
        score=float(cv2.matchTemplate(patch,template,cv2.TM_CCOEFF_NORMED).max())
        matches.append({'number':number,'score':score,'reference':spec['file']})
    matches.sort(key=lambda item:item['score'],reverse=True)
    if value is None and matches and matches[0]['score']>=.97 and (len(matches)==1 or matches[0]['score']-matches[1]['score']>=.03):
        number=matches[0]['number']
        # 带加号的实际OCR绝不被旧数字模板覆盖。
        if 1<=number<24 and not any('+' in text or '＋' in text for text in readings):value=number
    return {'value':value,'readings':readings,'box':list(box),'template_matches':matches[:3],
            'policy':'complete_plain_1_to_23_two_scale_or_full_reviewed_badge'}


class LazyEquipmentDocument:
    def __init__(self, path, output):
        self.path, self.output, self.document = path, output, None

    def find(self, *args, **kwargs):
        if self.document is None:
            self.document = OcrDocument(windows_ocr(self.path, Path(self.output) / 'ocr'))
        return self.document.find(*args, **kwargs)


class EquipmentVision:
    def __init__(self):
        assets = ROOT / 'assets/equipment'
        self.specs = json.loads((assets / 'sources.json').read_text(encoding='utf-8'))
        # [单件详情] 独立局部控件声明不改既有资产阈值与步骤向量身份。
        extra=assets/'detail_single_sources.json'
        if extra.is_file():self.specs.update(json.loads(extra.read_text(encoding='utf-8')))
        self.images = {name: read_image(assets / spec['file']) for name, spec in self.specs.items()}
        import numpy as np
        self.visibility = {}
        for name, image in self.images.items():
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            mask = gray >= max(1, np.percentile(gray, 70))
            self.visibility[name] = (mask, float(gray[mask].mean()) if mask.any() else 0)

    @staticmethod
    def normalized(image):
        return image if image.shape[:2] == (720, 1280) else cv2.resize(
            image, (1280, 720), interpolation=cv2.INTER_AREA)

    def anchor(self, image, name, threshold=.88):
        image = self.normalized(image)
        a, b, c, d = self.specs[name]['box']
        region = image[max(0, b-3):min(720, d+3), max(0, a-3):min(1280, c+3)]
        score = float(cv2.matchTemplate(region, self.images[name], cv2.TM_CCOEFF_NORMED).max())
        return score >= threshold

    def visible(self, image, name, minimum=.8):
        """A matching title dimmed behind a modal cannot identify the foreground."""
        image = self.normalized(image)
        a, b, c, d = self.specs[name]['box']
        mask, reference_mean = self.visibility[name]
        observed = cv2.cvtColor(image[b:d, a:c], cv2.COLOR_BGR2GRAY)
        return bool(mask.any() and float(observed[mask].mean()) >= minimum * reference_mean)

    def inspect_file(self, path, output, *, frame=None, include_inventory=True):
        image = read_image(path) if frame is None else frame
        doc = LazyEquipmentDocument(path, output)
        result=self.inspect(image,doc,output,include_inventory=include_inventory)
        # [前景] 使用已存在的详情标记/精炼按钮/关闭控件三证据，随后独立读分数与UR。
        if result.get('state')=='unknown':
            from return_navigation_vision import ReturnNavigationVision
            returned=ReturnNavigationVision().inspect(image)
            if returned and returned.get('state')=='equipment_detail':
                result={'state':'equipment_detail',**self.detail_flags(image,doc,output)}
        return result

    def detail_flags(self, image, doc, output=None):
        wear = doc.find('穿戴', (790, 470, 870, 510), exact=True)
        worn = doc.find('穿戴中', (320, 260, 550, 310))
        # [分数] 保留20模板兼容字段，同时从完整徽章独立读任意合法数字。
        output=Path(output or ROOT/'evidence/equipment_badge_ocr')
        numeric=read_plain_badge(image,self.specs['detail_score20']['box'],output,'detail_badge')
        # [品质] 只读取标题结尾品质字样，不以装备名称或武器造型分类；两尺度均为UR才成立。
        normalized=self.normalized(image);title_crop=normalized[215:250,320:700];quality_readings=[]
        for scale in (1,2):
            raw=cv2.imencode('.png',cv2.resize(title_crop,None,fx=scale,fy=scale))[1]
            path=output/('detail_quality_'+hashlib.sha256(raw.tobytes()).hexdigest()[:20]+'.png');path.parent.mkdir(parents=True,exist_ok=True)
            if not path.exists():write_encoded_png(path,raw)
            payload=windows_ocr(path,output/'ocr')
            quality_readings.append(compact(''.join(line['text'] for line in payload['lines'])))
        quality='UR' if all(re.search(r'UR$',text) for text in quality_readings) else None
        return {'quality':quality,'quality_evidence':quality_readings,'score':numeric['value'],'score_evidence':numeric,'unlocked': self.anchor(image, 'detail_unlocked', .97),
                'not_equipped': bool(wear and not worn and self.anchor(image, 'detail_wear_available', .97)),
                'score20': self.anchor(image, 'detail_score20', .97)}

    def detail_single_flags(self,image,doc,output):
        """[详情资格] 两尺度独立读品质后缀，不绑定装备名、不使用UR精炼分数。"""
        output=Path(output);output.mkdir(parents=True,exist_ok=True)
        normalized=self.normalized(image);readings=[]
        for scale in (2,3):
            crop=normalized[215:252,320:720]
            raw=cv2.imencode('.png',cv2.resize(crop,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC))[1]
            path=output/('single_detail_quality_'+hashlib.sha256(raw.tobytes()).hexdigest()[:20]+'.png')
            if not path.exists():write_encoded_png(path,raw)
            payload=windows_ocr(path,output/'ocr')
            text=compact(''.join(line['text'] for line in payload['lines']))
            match=re.search(r'(UR|SR|R|N)$',text)
            readings.append({'text':text,'quality':match[1] if match else None})
        quality=readings[0]['quality'] if readings[0]['quality']==readings[1]['quality'] else None
        wear=doc.find('穿戴',(790,470,870,510),exact=True)
        worn=doc.find('穿戴中',(320,250,700,470))
        return {'quality':quality,'quality_evidence':readings,'item_title':readings[0]['text'],
                'unlocked':self.anchor(image,'detail_unlocked',.97),
                'not_equipped':bool(wear and not worn and self.anchor(image,'detail_wear_available',.97)),
                'scissors_verified':self.anchor(image,'detail_single_scissors',.97),
                'detail_close_verified':self.anchor(image,'detail_single_close',.97),
                'points':{'decompose':[349,490]}}

    def inspect(self, image, doc, output, *, include_inventory=True):
        normalized = self.normalized(image)
        if abs(image.shape[1] / image.shape[0] - 16/9) > .035:
            return {'state': 'unknown', 'reason': 'unsupported_aspect_ratio'}
        if 'decompose_result_title' in self.images and self.anchor(normalized, 'decompose_result_title', .97):
            return {'state': 'equipment_decompose_result',
                    'matches_reviewed_55_5_result': self.anchor(normalized, 'decompose_result_rewards', .97)}
        if 'filter_title' in self.images and self.anchor(normalized, 'filter_title'):
            selected = ['R'] if self.anchor(normalized, 'filter_r_only', .97) else (
                ['N'] if 'filter_n_only' in self.images and self.anchor(normalized, 'filter_n_only', .97) else (
                [] if self.anchor(normalized, 'filter_quality_default', .97) else None))
            return {'state': 'equipment_filter', 'selected_qualities': selected,
                    'worn_hidden': self.anchor(normalized, 'filter_worn_off', .97),
                    'default_visible_options': self.anchor(normalized, 'filter_quality_default', .97)
                        and self.anchor(normalized, 'filter_other_default', .97)
                        and self.anchor(normalized, 'filter_worn_default', .97),
                    'saved_toast': self.anchor(normalized, 'filter_saved_toast', .97)}
        if 'decompose_confirm_warning' in self.images and self.anchor(normalized, 'decompose_confirm_warning', .97):
            return {'state': 'equipment_decompose_confirmation',
                    'matches_reviewed_one_item_confirmation': self.anchor(normalized, 'decompose_confirm_one', .97)}
        if 'decompose_select_prompt' in self.images and self.anchor(normalized, 'decompose_select_prompt'):
            return {'state': 'equipment_decompose_selection',
                    'matches_reviewed_one_item_selection': self.anchor(normalized, 'selected_count_one', .97)}
        # [普通详情] 剪刀/关闭控件定位页面，品质及锁定穿戴仍各自独立读取。
        if ('detail_single_scissors' in self.images and self.anchor(normalized,'detail_single_scissors',.97)
                and self.anchor(normalized,'detail_single_close',.97)):
            flags=self.detail_single_flags(normalized,doc,output)
            if flags['quality'] in ('R','N'):
                return {'state':'equipment_detail','detail_basis':'detail_single_item',**flags}
        if 'detail_r_gun' in self.images and self.anchor(normalized, 'detail_r_gun', .97):
            return {'state': 'equipment_reviewed_R_detail', 'quality': 'R',
                    **self.detail_flags(normalized, doc, output)}
        if ('refine_title' in self.images and self.anchor(normalized, 'refine_single')
                and (self.anchor(normalized, 'refine_title') or
                     (self.anchor(normalized, 'refine_score_label') and self.anchor(normalized, 'refine_material_label')))):
            fields = {name: read_integer(image, box, output, name) for name, box in {
                'material_balance': (989, 25, 1068, 49), 'gold_balance': (1099, 25, 1200, 49),
                'material_cost': (1020, 592, 1048, 617), 'gold_cost': (1088, 658, 1118, 681),
            }.items()}
            # [分数] 精炼页同样读取完整纯数字，不把20旧图当唯一允许分数。
            score_evidence=read_plain_badge(image,self.specs['refine_score20']['box'],output,'refine_badge')
            score=score_evidence['value']
            return {'state': 'equipment_refine', 'score': score, 'score_evidence':score_evidence,'numeric_evidence': fields,
                    'before': {'material': fields['material_balance']['value'], 'gold': fields['gold_balance']['value']},
                    'cost': {'material': fields['material_cost']['value'], 'gold': fields['gold_cost']['value']},
                    'points': {'submit': [1070, 670], 'back': [113, 33]}}
        if all(name in self.images and self.anchor(normalized, name, .94) and self.visible(normalized, name)
               for name in ('bag_consumables_title', 'bag_consumables_selected')):
            return {'state': 'equipment_bag', 'recognition': 'title_and_selected_bag_icon'}
        if self.anchor(normalized, 'grid_title', .97) and self.visible(normalized, 'grid_title'):
            return {'state': 'equipment_grid', 'inventory': read_inventory_count(image, output) if include_inventory else {'value': None, 'deferred': True},
                    'empty_filtered_grid': 'grid_empty' in self.images and self.anchor(normalized, 'grid_empty', .97)}
        from ui_foreground import MenuForegroundRecognizer
        if MenuForegroundRecognizer().inspect(image)['state'] == 'main_menu':
            return {'state': 'main_menu'}
        return {'state': 'unknown'}
