"""[只读识别] 主页固定字幕认解锁；进入中心后独立识别活动身份和普通/挑战入口。"""
import json
import hashlib
import re
from pathlib import Path

import cv2

from daily_vision import OcrDocument, compact, windows_ocr
from m00_recognize import ROOT, read_image
from phase08_vision import Phase08Vision


def read_free_ap(image, output):
    normalized = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)
    crop = cv2.cvtColor(normalized[25:44, 1165:1193], cv2.COLOR_BGR2GRAY)
    _, crop = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    crop = cv2.copyMakeBorder(crop, 6, 6, 48, 6, cv2.BORDER_CONSTANT, value=0)
    cv2.putText(crop, 'AP', (4, 21), cv2.FONT_HERSHEY_SIMPLEX, .60, 255, 1, cv2.LINE_AA)
    readings, values = [], []
    for scale in (4, 6):
        encoded = cv2.imencode('.png', cv2.resize(crop, None, fx=scale, fy=scale,
                                                interpolation=cv2.INTER_CUBIC))[1]
        path = Path(output) / ('free_ap_' + hashlib.sha256(encoded.tobytes()).hexdigest()[:18] + '.png')
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded.tofile(str(path))
        payload = windows_ocr(path, Path(output) / 'ocr', language='en-US')
        text = compact(''.join(line['text'] for line in payload['lines']))
        readings.append(text)
        match = re.fullmatch(r'(?:AP)?(\d+)/(\d+)', text, flags=re.IGNORECASE)
        value = (int(match[1]), int(match[2])) if match else None
        values.append(value if value and 0 <= value[0] <= value[1] and value[1] > 0 else None)
    value = values[0] if values[0] is not None and values[0] == values[1] else None
    return {'value': value[0] if value else None, 'capacity': value[1] if value else None,
            'readings': readings}


class EventPreparationVision:
    def __init__(self):
        folder = ROOT / 'assets/event_prepare'
        self.specs = json.loads((folder / 'sources.json').read_text(encoding='utf-8'))
        self.templates = {name: read_image(folder / spec['file']) for name, spec in self.specs.items()}
        self.base = Phase08Vision()

    def anchor(self, image, name, threshold=.90):
        if name not in self.templates:
            return False
        image = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)
        a, b, c, d = self.specs[name]['box']
        region = image[max(0, b-4):min(720, d+4), max(0, a-4):min(1280, c+4)]
        return float(cv2.matchTemplate(region, self.templates[name], cv2.TM_CCOEFF_NORMED).max()) >= threshold

    def home_event(self, image, payload=None):
        """[功能] 入口只要求固定区域解锁两字；标题不作为主页进入条件。"""
        if payload is not None:
            from event_generic_vision import generic_event_observation
            generic=generic_event_observation(image,payload,'main_menu')
            if generic:
                return {**generic,'point':generic['points']['enter_event']}
        return None

    def inspect_file(self, path, output):
        image = read_image(path)
        # [主页快分支] 全图只核主页图形，字幕单独放大OCR，不让logo读取阻挡入口。
        from common_navigation import PageDiagnosis
        if PageDiagnosis().inspect(image)['state']=='main_menu':
            from event_entry_vision import read_unlock_entry
            return read_unlock_entry(image,Path(output)/'home_entry') or {'state':'main_menu','points':{}}
        payload = windows_ocr(path, Path(output) / 'ocr')
        observation = self.base.inspect_file(path, output, payload)
        # This adapter has no consuming action. Do not expose a submit point.
        observation.get('points', {}).pop('submit', None)
        if observation['state'] in ('event_preparation', 'event_auto_dialog'):
            numeric = read_free_ap(image, output)
            observation['free_ap_evidence'] = numeric
            # Only the current adapter's complete token is used, not a nearest old template.
            observation['free_ap'] = numeric['value']
            if observation['state'] == 'event_preparation':
                observation['points']['back'] = [113, 33]
        if observation['state'] == 'main_menu':
            entry = self.home_event(image,payload)
            if entry:
                if entry.get('event_id'):observation['event_id'] = entry['event_id']
                observation['points'] = {'enter_event': entry['point']}
                observation['event_entry_evidence'] = entry
            return observation
        # [功能] 独立只读适配器也使用同一通用标题/控件规则，避免与执行视觉分叉。
        from event_generic_vision import generic_event_observation
        generic=generic_event_observation(image,payload,observation['state'])
        if generic:return generic
        if self.anchor(image, 'hub_loyal_maid'):
            doc = OcrDocument(payload)
            normal = doc.find('普通战斗', (300, 600, 500, 670))
            challenge = doc.find('挑战战斗', (490, 600, 630, 670))
            if normal and challenge:
                return {'state': 'event_hub', 'event_id': 'Loyal_Maid_20261016',
                        'points': {'normal': normal['point'], 'challenge': challenge['point'], 'back': [113, 33]}}
        return observation
