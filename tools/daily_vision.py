"""Local Windows OCR and small UI anchors. Reads images, never sends input."""
import hashlib
import json
import re
import subprocess
import sys
import threading
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path

import cv2
import numpy as np

from m00_recognize import ROOT, read_image
from ui_foreground import foreground_mask, masked_shape_match

_external_process_lock = threading.Lock()
_numeric_crop_lock = threading.Lock()


def compact(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))


def read_amount_crop(image, box, output, name):
    """Two independent sizes must agree on the displayed amount."""
    normalized = cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
    a,b,c,d=box
    gray=cv2.cvtColor(normalized[b:d,a:c],cv2.COLOR_BGR2GRAY)
    _,gray=cv2.threshold(gray,0,255,cv2.THRESH_BINARY|cv2.THRESH_OTSU)
    # Windows OCR often ignores a single glyph. A known "00" prefix makes it
    # readable; require and remove that prefix, never treating it as game data.
    prefix_width=50
    original_height=gray.shape[0]
    background=255 if float(np.median(np.concatenate((gray[0],gray[-1]))))>127 else 0
    gray=cv2.copyMakeBorder(gray,8,8,prefix_width,8,cv2.BORDER_CONSTANT,value=background)
    cv2.putText(gray,'00',(4,8+int(original_height*.78)),cv2.FONT_HERSHEY_SIMPLEX,
                .6,255-background,1,cv2.LINE_AA)
    values=[]
    for scale in (4,6):
        encoded=cv2.imencode('.png',cv2.resize(gray,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC))[1]
        path=Path(output)/f'{name}_{hashlib.sha256(encoded.tobytes()).hexdigest()[:16]}_{scale}.png'
        path.parent.mkdir(parents=True,exist_ok=True)
        with _numeric_crop_lock:
            if not path.exists():
                encoded.tofile(str(path))
            payload=windows_ocr(path,Path(output)/'ocr',language='en-US')
        text=compact(''.join(line['text'] for line in payload['lines'])).replace(',','')
        text=text[2:] if text.startswith('00') else ''
        match=re.fullmatch(r'(\d+(?:\.\d+)?)([KkMm]?)',text)
        try:
            value=int(Decimal(match[1])*({'':1,'K':1000,'M':1000000}[match[2].upper()])) if match else None
        except (InvalidOperation,ValueError):
            value=None
        values.append(value)
    return values[0] if values[0] is not None and values[0]==values[1] else None


def windows_ocr(path, output, language='zh-Hans-CN'):
    path, output = Path(path).resolve(strict=True), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    target = output / (f'{sha}.json' if language == 'zh-Hans-CN' else f'{sha}_{language}.json')
    if not target.exists():
        command = ['C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe', '-NoProfile',
                   '-ExecutionPolicy', 'Bypass', '-File', str(ROOT / 'tools/windows_ocr.ps1'),
                   '-InputPath', str(path), '-OutputPath', str(target), '-LanguageTag', language]
        # PyInstaller adds its bundled DLL directory to Windows' search order.
        # System PowerShell/WinRT must load the operating system's own DLLs.
        if getattr(sys, 'frozen', False):
            import ctypes
            with _external_process_lock:
                ctypes.windll.kernel32.SetDllDirectoryW(None)
                try:
                    result = subprocess.run(command, capture_output=True, timeout=25,
                                            creationflags=subprocess.CREATE_NO_WINDOW)
                finally:
                    ctypes.windll.kernel32.SetDllDirectoryW(str(sys._MEIPASS))
        else:
            result = subprocess.run(command, capture_output=True, timeout=25,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            (output / f'{sha}.stderr.bin').write_bytes(result.stderr)
            raise RuntimeError(f'Windows OCR failed; original error bytes saved for {sha}')
    return json.loads(target.read_text(encoding='utf-8'))


def read_mirror_multiplier(image,box,output,name):
    normalized=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
    a,b,c,d=box
    gray=cv2.cvtColor(normalized[b:d,a:c],cv2.COLOR_BGR2GRAY)
    _,gray=cv2.threshold(gray,0,255,cv2.THRESH_BINARY|cv2.THRESH_OTSU)
    height=gray.shape[0]
    background=255 if float(np.median(np.concatenate((gray[0],gray[-1]))))>127 else 0
    gray=cv2.copyMakeBorder(gray,8,8,50,8,cv2.BORDER_CONSTANT,value=background)
    cv2.putText(gray,'00',(4,8+int(height*.78)),cv2.FONT_HERSHEY_SIMPLEX,.6,255-background,1,cv2.LINE_AA)
    values=[]
    for scale in (4,6):
        encoded=cv2.imencode('.png',cv2.resize(gray,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC))[1]
        path=Path(output)/f'{name}_{hashlib.sha256(encoded.tobytes()).hexdigest()[:16]}_{scale}.png'
        path.parent.mkdir(parents=True,exist_ok=True)
        with _numeric_crop_lock:
            if not path.exists():encoded.tofile(str(path))
            payload=windows_ocr(path,Path(output)/'ocr',language='en-US')
        text=compact(''.join(line['text'] for line in payload['lines']))
        # OCR sometimes renders the trailing 倍 as fi. Only this fixed label
        # accepts that suffix; prices and balances keep their strict parser.
        match=re.fullmatch(r'00(\d{1,2})(?:倍|[fFiIlL]+)?',text)
        value=int(match[1]) if match else None
        values.append(value if value is not None and 1<=value<=40 else None)
    return values[0] if values[0] is not None and values[0]==values[1] else None


def read_mirror_price(image,box,output):
    image=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
    a,b,c,d=box;gray=cv2.cvtColor(image[b:d,a:c],cv2.COLOR_BGR2GRAY)
    _,gray=cv2.threshold(gray,0,255,cv2.THRESH_BINARY|cv2.THRESH_OTSU)
    height=gray.shape[0];background=255 if float(np.median(gray))>127 else 0
    gray=cv2.copyMakeBorder(gray,8,8,84,8,cv2.BORDER_CONSTANT,value=background)
    cv2.putText(gray,'VALUE',(3,8+int(height*.78)),cv2.FONT_HERSHEY_SIMPLEX,.60,255-background,1,cv2.LINE_AA)
    values=[];texts=[]
    for scale in (3,4):
        encoded=cv2.imencode('.png',cv2.resize(gray,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC))[1]
        path=Path(output)/f'mirror_value_{hashlib.sha256(encoded.tobytes()).hexdigest()[:16]}_{scale}.png'
        path.parent.mkdir(parents=True,exist_ok=True)
        with _numeric_crop_lock:
            if not path.exists():encoded.tofile(str(path))
            payload=windows_ocr(path,Path(output)/'ocr',language='en-US')
        text=compact(''.join(line['text'] for line in payload['lines'])).upper();texts.append(text)
        match=re.fullmatch(r'VALUE([0-9OIL]+)',text)
        digits=match[1].replace('O','0').replace('I','1').replace('L','1') if match else ''
        value=int(digits) if digits else None
        values.append(value if value is not None and 1<=value<=40 else None)
    return (values[0] if values[0] is not None and values[0]==values[1] else None),texts


def inspect_frame(path, vision, config, output, payload=None):
    path, output = Path(path), Path(output)
    payload = payload if payload is not None else windows_ocr(path, output / 'ocr')
    image = read_image(path)
    result = vision.inspect(image, payload, config)
    if result['state']=='hunt_dialog' and result.get('cost') is None:
        result['cost']=read_amount_crop(image,(712,508,747,537),output,'hunt_price')
        result['price_source']='two_size_numeric_crop' if result['cost'] is not None else None
    if result['state']=='hunt_map' and result.get('stone_map'):
        wants_min=any(p.get('target_mode')=='lowest_stock' for p in config['hunting']['extra_plans'])
        if wants_min and not result.get('stone_inventory'):
            amounts={}
            for stage,spec in vision.stone_specs.items():
                if not vision.stone_icon(image,stage):
                    break
                value=read_amount_crop(image,spec['amount_box'],output,f'stone_{list(vision.stone_specs).index(stage)}')
                if value is None:
                    break
                amounts[stage]=value
            result['stone_inventory']=amounts if len(amounts)==5 else None
    if result['state'] == 'mirror_auto_dialog' and result.get('cost') is None:
        # Read only the price to the right of the currency icon; never mistake
        # the multiplier printed at the start of the button for its total cost.
        normalized = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)
        crop = cv2.cvtColor(normalized[532:552, 740:766], cv2.COLOR_BGR2GRAY)
        _, crop = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        crop = cv2.copyMakeBorder(crop, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)
        values = []
        output.mkdir(parents=True, exist_ok=True)
        source_hash = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        for scale in (3, 4):
            crop_path = output / f'price_{source_hash}_{scale}.png'
            enlarged = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            cv2.imencode('.png', enlarged)[1].tofile(str(crop_path))
            price_ocr = windows_ocr(crop_path, output / 'ocr', language='en-US')
            text = compact(''.join(l['text'] for l in price_ocr['lines']))
            values.append(int(text) if re.fullmatch(r'\d+', text) else None)
        result['price_crop_readings'] = values
        if values[0] is not None and values[0] > 0 and values[0] == values[1]:
            result['cost'] = values[0]
            result['price_source'] = 'same_frame_numeric_crop_agreement'
        if result.get('cost') is None:
            result['cost']=read_amount_crop(image,(740,532,766,552),output,'mirror_price')
            result['price_source']='two_size_prefixed_numeric_crop' if result['cost'] is not None else None
    if result['state'] in ('mirror_auto_dialog','mirror_multiplier_dialog'):
        field='auto' if result['state']=='mirror_auto_dialog' else 'editor'
        label=vision.mirror_entry.fixed_number(image,field)
        if label:
            if result.get('multiplier') is not None and result['multiplier']!=label['value']:
                result['multiplier']=None;result['multiplier_conflict']=True
            else:
                result['multiplier']=label['value'];result['multiplier_source']=label
    if result['state']=='mirror_auto_dialog' and result.get('multiplier') is None and not result.get('multiplier_conflict'):
        result['multiplier']=read_mirror_multiplier(image,(828,221,855,243),output,'mirror_multiplier')
        result['multiplier_source']='two_size_numeric_crop' if result['multiplier'] is not None else None
    if result['state']=='mirror_multiplier_dialog' and result.get('multiplier') is None and not result.get('multiplier_conflict'):
        result['multiplier']=read_mirror_multiplier(image,(630,381,660,404),output,'mirror_multiplier_editor')
    if result['state']=='mirror_auto_dialog':
        price_box=vision.mirror_entry.price_box(image)
        if price_box:
            current,readings=read_mirror_price(image,price_box,output)
            result['price_box']=price_box
            result['price_readings']=readings
            if current is not None:
                if result.get('cost') is not None and result['cost']!=current:
                    result['cost']=None;result['price_source']='conflicting_price_reads'
                else:
                    result['cost']=current;result['price_source']='currency_icon_and_two_size_digits'
        label=vision.mirror_entry.fixed_number(image,'price')
        if label:
            if result.get('cost') is not None and result['cost']!=label['value']:
                result['cost']=None;result['price_source']='conflicting_price_reads'
            elif result.get('price_source')!='conflicting_price_reads':
                result['cost']=label['value'];result['price_source']=label
    return result


class OcrDocument:
    def __init__(self, payload):
        self.lines = []
        width, height = payload['width'], payload['height']
        for line in payload['lines']:
            words = line['words']
            if not words:
                continue
            x1, y1 = min(w['x'] for w in words), min(w['y'] for w in words)
            x2 = max(w['x'] + w['width'] for w in words)
            y2 = max(w['y'] + w['height'] for w in words)
            self.lines.append({'text': compact(line['text']),
                               'box': [x1 * 1280 / width, y1 * 720 / height,
                                       x2 * 1280 / width, y2 * 720 / height],
                               'point': [(x1+x2)*640/width, (y1+y2)*360/height]})

    def within(self, roi):
        a, b, c, d = roi
        return [l for l in self.lines if a <= l['point'][0] <= c and b <= l['point'][1] <= d]

    def find(self, text, roi, exact=False):
        text = compact(text)
        candidates = [l for l in self.within(roi)
                      if (l['text'] == text if exact else text in l['text'])]
        return candidates[0] if len(candidates) == 1 else None

    def extract_int(self, pattern, roi):
        values = [int(m.group(1)) for l in self.within(roi) if (m := re.search(pattern, l['text']))]
        return values[0] if len(values) == 1 else None


class DailyVision:
    def __init__(self):
        assets = ROOT / 'assets/daily_pending'
        self.specs = json.loads((assets / 'config.json').read_text(encoding='utf-8'))
        self.images = {name: read_image(assets / spec['file']) for name, spec in self.specs.items()}
        options=ROOT/'assets/hunting_options'
        self.stone_specs=json.loads((options/'sources.json').read_text(encoding='utf-8'))['stones'] if (options/'sources.json').exists() else {}
        self.stone_images={name:read_image(options/spec['file']) for name,spec in self.stone_specs.items()}
        difficulty_specs=json.loads((options/'sources.json').read_text(encoding='utf-8')).get('difficulties',{}) if (options/'sources.json').exists() else {}
        self.difficulty_images={name:read_image(options/spec['file']) for name,spec in difficulty_specs.items()}
        self.home_label=read_image(options/'home_hunt_label.png') if (options/'home_hunt_label.png').exists() else None
        from goddess_entry_vision import GoddessEntryVision
        self.goddess_entry=GoddessEntryVision()
        from mirror_entry_vision import MirrorEntryVision
        self.mirror_entry=MirrorEntryVision()

    def current_hunt_entry(self,image):
        if self.home_label is None:
            return None
        gray=cv2.cvtColor(cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA),cv2.COLOR_BGR2GRAY)
        template=cv2.cvtColor(self.home_label,cv2.COLOR_BGR2GRAY)
        patch=cv2.Laplacian(template,cv2.CV_32F)
        region=cv2.Laplacian(gray[182:225,1134:1241],cv2.CV_32F)
        score=float(cv2.matchTemplate(region,patch,cv2.TM_CCOEFF_NORMED).max())
        return [1183,170] if score>=.80 else None

    def hunt_difficulty(self,image):
        normalized=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
        region=cv2.cvtColor(normalized[638:680,210:270],cv2.COLOR_BGR2GRAY)
        region=(region>=max(50,float(region.max())*.80)).astype('uint8')*255
        scores=[]
        for level,source in self.difficulty_images.items():
            gray=cv2.cvtColor(source,cv2.COLOR_BGR2GRAY)
            template=(gray>=float(gray.max())*.80).astype('uint8')*255
            best=-1
            for factor in (.55,.60,.65,.70,.75,.80,.85,.90,.95,1.0,1.05):
                patch=cv2.resize(template,None,fx=factor,fy=factor,interpolation=cv2.INTER_NEAREST)
                if patch.shape[0]>region.shape[0] or patch.shape[1]>region.shape[1]:
                    continue
                best=max(best,float(cv2.matchTemplate(region,patch,cv2.TM_CCOEFF_NORMED).max()))
            scores.append((best,level))
        scores.sort(reverse=True)
        self.last_difficulty_scores=scores
        return scores[0][1] if len(scores)>=2 and scores[0][0]>=.65 and scores[0][0]-scores[1][0]>=.05 else None

    def stone_icon(self,image,name):
        normalized=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
        a,b,c,d=self.stone_specs[name]['box']
        roi=normalized[b-3:d+3,a-3:c+3]
        score=float(cv2.matchTemplate(roi,self.stone_images[name],cv2.TM_CCOEFF_NORMED).max())
        return score>=0.80

    def anchor(self, image, name, threshold=0.76, foreground=True, dark=False):
        normalized = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY)
        template = cv2.cvtColor(self.images[name], cv2.COLOR_BGR2GRAY)
        a, b, c, d = self.specs[name]['box_1280']
        pad = self.specs[name].get('search_padding', 12)
        left, top, right, bottom = max(0, a-pad), max(0, b-pad), min(1280, c+pad), min(720, d+pad)
        region = gray[top:bottom, left:right]
        result = None
        for factor in (0.96, 1.0, 1.04):
            patch = cv2.resize(template, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
            if foreground:
                mask = foreground_mask(patch)
                match = masked_shape_match(255-region if dark else region, 255-mask if dark else mask)
                score, pos = match['score'], match['position']
            else:
                _, score, _, pos = cv2.minMaxLoc(cv2.matchTemplate(region, patch, cv2.TM_CCOEFF_NORMED))
            x, y = pos
            ph, pw = patch.shape
            ratio = float(region[y:y+ph, x:x+pw].mean() / max(patch.mean(), 1))
            if result is None or score > result['score']:
                result = {'score': float(score), 'brightness_ratio': ratio,
                          'point': [left+x+pw/2, top+y+ph/2]}
        return result if result and result['score'] >= threshold else None

    @staticmethod
    def toggle(image, box):
        normalized = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)
        a, b, c, d = box
        hsv = cv2.cvtColor(normalized[b:d, a:c], cv2.COLOR_BGR2HSV)
        yellow = float(np.mean((hsv[:, :, 0] >= 15) & (hsv[:, :, 0] <= 40)
                               & (hsv[:, :, 1] > 45) & (hsv[:, :, 2] > 45)))
        # Both a neutral knob and a track must exist before interpreting off.
        knob = float(np.mean((hsv[:, :, 1] < 50) & (hsv[:, :, 2] > 65)))
        if yellow >= 0.20 and knob >= 0.06:
            return True
        if yellow < 0.03 and knob >= 0.10:
            return False
        return None

    def inspect(self, image, payload, config):
        if abs(image.shape[1] / image.shape[0] - 16/9) > 0.035:
            return {'state': 'unknown', 'reason': 'unsupported_aspect_ratio'}
        doc = OcrDocument(payload)
        collection=self.goddess_entry.library(image,doc)
        if collection:
            collection['mirror_entry_point']=self.mirror_entry.card_point(image,doc)
            return collection
        multiplier_dialog=self.mirror_entry.multiplier_dialog(image,doc)
        if multiplier_dialog:return multiplier_dialog
        quick = self.anchor(image, 'hunt_title')
        if quick and doc.find('快速狩猎', (390, 150, 550, 240)):
            dx, dy = quick['point'][0] - 471, quick['point'][1] - 204.5
            stage = doc.within((390, 210, 570, 250))
            stage = stage[0]['text'] if len(stage) == 1 else None
            count = doc.extract_int(r'^狩猎(\d+)次$', (550, 245, 740, 305))
            button_lines = doc.within((620, 485, 810, 560))
            costs = [int(m.group(1)) for l in button_lines if '狩猎' in l['text']
                     and (m := re.search(r'(\d+)$', l['text']))]
            resource = ('torch' if doc.find('免费火把', (650, 160, 870, 235)) else
                        'rice' if doc.find('免费米饭', (650, 160, 870, 235)) else None)
            return {'state': 'hunt_dialog', 'stage_text': stage, 'count': count,
                    'cost': costs[0] if len(costs) == 1 else None, 'resource': resource,
                    'free_available': doc.extract_int(r'(\d+)/\d+', (1000, 0, 1225, 85)),
                    'free_only': self.toggle(image, tuple(round(v+shift) for v, shift in
                                                          zip((812, 187, 850, 217), (dx, dy, dx, dy)))),
                    'points': {key: [x+dx, y+dy] for key, (x, y) in {
                        'min': [500, 341], 'plus': [814, 389], 'minus': [463, 389],
                        'submit': [687, 522], 'cancel': [536, 522], 'free_toggle': [830, 204]}.items()}}
        if self.anchor(image, 'mirror_auto_title') and doc.find('鲜血鸡尾酒', (370, 185, 620, 232)):
            count = doc.extract_int(r'^自动战斗(\d+)次$', (555, 430, 740, 485))
            multiplier = doc.extract_int(r'^(\d+)倍', (800, 190, 900, 260))
            cost_lines = doc.within((605, 505, 835, 565))
            if multiplier is None:
                prefix_values=[int(m[1]) for line in cost_lines
                               if (m:=re.match(r'^(\d{1,2})倍战斗开始',line['text']))]
                if len(prefix_values)==1 and 1<=prefix_values[0]<=40:
                    multiplier=prefix_values[0]
            costs = [int(m.group(1)) for l in cost_lines
                     if (m := re.search(r'战斗开始[^\d]*(\d+)[^\d]*$', l['text']))]
            # OCR may separate the price into its own line; do not guess the sum.
            cost = costs[0] if len(costs) == 1 else None
            return {'state': 'mirror_auto_dialog', 'count': count, 'multiplier': multiplier,
                    'cost': cost, 'free_available': doc.extract_int(r'(\d+)/\d+', (1000, 0, 1225, 85)),
                    'free_only': self.toggle(image, (848, 268, 887, 291)),
                    'auto_skip': self.toggle(image, (848, 333, 887, 356)),
                    'points': {'min': [462, 425], 'plus': [870, 493], 'minus': [416, 493],
                               'submit': [689, 541], 'cancel': [535, 541],
                               'free_toggle': [868, 279], 'skip_toggle': [868, 344],
                               'multiplier_settings': [873, 232]}}
        result_title = self.anchor(image, 'mirror_result_title')
        if result_title and doc.find('反复战斗结果', (540, 195, 740, 250)):
            count = doc.extract_int(r'已完成(\d+)次', (530, 345, 750, 405))
            return {'state': 'mirror_result', 'completed_battles': count,
                    'close_point': [793, 204]}
        reward = self.anchor(image, 'hunt_reward')
        if reward and doc.find('获得', (535, 225, 755, 285)):
            count = doc.extract_int(r'狩猎(\d+)次', (560, 200, 750, 300))
            return {'state': 'hunt_reward', 'completed_runs': count, 'dismiss_point': [770, 656],
                    'free_remaining': doc.extract_int(r'(\d+)/\d+', (1000, 0, 1225, 85))}
        ready_auto = self.anchor(image, 'mirror_auto_entry')
        ready_battle = self.anchor(image, 'mirror_battle', foreground=False)
        if ready_auto and ready_battle and min(ready_auto['brightness_ratio'], ready_battle['brightness_ratio']) >= 0.78:
            return {'state': 'mirror_ready', 'open_auto_point': [1015, 649]}
        if (self.anchor(image, 'mirror_result_leave') and doc.find('离开', (1065, 630, 1210, 700))):
            return {'state': 'mirror_result_footer', 'leave_point': [1140, 672]}
        lobby=self.mirror_entry.lobby(image)
        if lobby: return lobby
        hand = self.anchor(image, 'goddess_hand', threshold=0.73, dark=True)
        daily_ready = self.anchor(image, 'goddess_reward_ready', threshold=0.74, dark=True)
        # Explicit user pre-positioning is a precondition. The two glyphs occupy
        # the interaction button, never NPC markers elsewhere on the screen.
        if config['goddess']['user_positioned_near_statue'] and (hand or daily_ready):
            return {'state': 'goddess_options',
                    'offers': [{'action': 'check_in' if hand else 'daily_reward',
                                'label': 'hand' if hand else 'interaction_exclamation'}],
                    'interaction_point': [1280*config['goddess']['interaction_point_normalized'][0],
                                          720*config['goddess']['interaction_point_normalized'][1]]}
        offers = []
        for action, labels in config['goddess']['labels'].items():
            for label in labels:
                match = doc.find(label, (560, 65, 1040, 270))
                if match:
                    offers.append({'action': action, 'label': label, 'point': match['point']})
        if offers and self.anchor(image, 'goddess_interact', threshold=0.68, foreground=False):
            return {'state': 'goddess_options', 'offers': offers,
                    'interaction_point': [1280*config['goddess']['interaction_point_normalized'][0],
                                          720*config['goddess']['interaction_point_normalized'][1]]}
        # Completion toast and loot are evidence only for an already submitted action.
        goddess_done = doc.find('向女神像许愿1/1完成', (700, 0, 1100, 85))
        if goddess_done:
            return {'state': 'goddess_check_in_result'}
        if self.anchor(image, 'goddess_reward_loot', foreground=False, threshold=0.84):
            return {'state': 'goddess_reward_result', 'evidence': 'recorded_reward_icon'}
        loot = [l for l in doc.within((30, 270, 425, 550)) if re.search(r'[×xX]\d+', l['text'])]
        if loot and doc.find('创建队伍', (1050, 55, 1230, 140)):
            return {'state': 'goddess_loot_result', 'loot': loot}
        if (doc.find('创建队伍',(1010,65,1245,145))
                or self.goddess_entry.match(image,'square_team',foreground=False,threshold=.85)):
            from common_navigation import PageDiagnosis
            verified_scene=bool(doc.find('向女神像许愿',(620,70,950,160)) or PageDiagnosis().find_home(image))
        else:
            verified_scene=False
        if verified_scene:
            return {'state':'goddess_scene','interaction_available':False,
                    'evidence':'team_creation_UI_and_verified_scene_navigation'}
        map_quick = self.anchor(image, 'hunt_map_quick', threshold=0.73, foreground=False)
        if map_quick and map_quick['brightness_ratio'] >= 0.80:
            selected = doc.within((140, 10, 370, 85))
            categories = {k: m['point'] for k in ('狩猎场', '金币', '史莱姆', '圣石洞穴')
                          if (m := doc.find(k, (65, 100, 160, 355)))}
            difficulties=[value for value in ('中游','困难','极难') if doc.find(value,(155,620,290,690))]
            stone_points=[line for line in doc.within((150,65,295,320))
                          if line['text'] in ('火之洞穴','水之洞穴','风之洞穴','光之洞穴','暗之洞穴')]
            stone_map=bool(doc.find('圣石洞穴',(140,10,375,85)))
            inventory={}
            if stone_map:
                for index,name in enumerate(('火之洞穴','水之洞穴','风之洞穴','光之洞穴','暗之洞穴')):
                    lines=doc.within((1130,54+28*index,1205,82+28*index))
                    values=[]
                    for line in lines:
                        match=re.search(r'(\d[\d,.]*\d|\d)([KkMm]?)',line['text'])
                        if not match:
                            continue
                        number=match[1].rstrip('.,')
                        if not match[2]:
                            if '.' in number and not re.fullmatch(r'\d{1,3}(?:\.\d{3})+',number):
                                continue
                            number=number.replace('.','').replace(',','')
                        else:
                            number=number.replace(',','')
                        try:
                            values.append(int(Decimal(number)*{'':1,'K':1000,'M':1000000}[match[2].upper()]))
                        except InvalidOperation:
                            continue
                    if len(values)==1 and self.stone_icon(image,name):
                        inventory[name]=values[0]
            return {'state': 'hunt_map', 'selected_text': selected[0]['text'] if selected else None,
                    'difficulty': difficulties[0] if len(difficulties)==1 else self.hunt_difficulty(image),
                    'difficulty_points':{'previous':[165,659],'next':[317,659]},
                    'stone_map':stone_map,'stone_stage_points':stone_points,
                    'stone_inventory':inventory if len(inventory)==5 else None,
                    'category_points': categories, 'open_quick_point': [1105, 659],
                    'back_point': [113, 33],
                    'nodes': doc.within((250, 80, 1060, 600)),
                    'daily_task_done': any('在狩猎场' in l['text'] and ('1/1完成' in l['text'] or ('冒险航线' in l['text'] and '1/1' in l['text']))
                                           for l in doc.within((600, 0, 1100, 80)))}
        from ui_foreground import MenuForegroundRecognizer
        if MenuForegroundRecognizer().inspect(image)['state'] == 'main_menu':
            entry = self.anchor(image, 'hunt_home_entry')
            text_entry=doc.find('快速狩猎',(1050,100,1275,260))
            return {'state': 'main_menu', 'hunt_entry_point': entry['point'] if entry else text_entry['point'] if text_entry else self.current_hunt_entry(image),
                    'collection_entry_point':self.goddess_entry.collection_point(image,doc)}
        return {'state': 'unknown', 'reason': 'no_supported_actionable_ui'}
