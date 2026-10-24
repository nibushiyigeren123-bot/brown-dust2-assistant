"""Event UI observations. Exposing a point never authorizes a submission."""
from reward_overlay_vision import reward_dismiss_point
import re
import json
from pathlib import Path

import cv2

from daily_vision import DailyVision, OcrDocument, windows_ocr, read_amount_crop
from event_prepare_vision import EventPreparationVision
from m00_recognize import read_image, ROOT


# [资源] 所有活动布局统一采用固定右上X/5合同。
from event_ap_vision import resource_balance


def button_cost(doc, roi):
    values = []
    for row in doc.within(roi):
        match = re.fullmatch(r'战斗[^\d]{0,3}(\d+)', row['text'])
        if match:
            values.append((int(match[1]), row['point']))
    return values[0] if len(values) == 1 else (None, None)


class EventBattleVision:
    def __init__(self):
        self.preparation = EventPreparationVision()
        specs = json.loads((ROOT / 'assets/event_battle/config.json').read_text(encoding='utf-8'))
        self.auto_spec = specs['auto_entry']
        self.auto_template = read_image(ROOT / 'assets/event_battle' / self.auto_spec['file'])
        self.unit_templates = [(spec, read_image(ROOT / 'assets/event_battle' / spec['file']))
                               for spec in specs.get('unit_prices', [])]

    def auto_entry(self, image):
        normalized = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)
        a, b, c, d = self.auto_spec['box']
        region = normalized[b-4:d+4, a-4:c+4]
        score = float(cv2.matchTemplate(region, self.auto_template, cv2.TM_CCOEFF_NORMED).max())
        return self.auto_spec['point'] if score >= .94 else None

    def inspect(self, image, payload):
        if abs(image.shape[1] / image.shape[0] - 16 / 9) > .025:
            return {'state': 'unknown', 'reason': 'unsupported_aspect_ratio'}
        doc = OcrDocument(payload)
        balance = resource_balance(doc)
        quick = doc.find('快速战斗', (550, 155, 735, 210), exact=True)
        auto = doc.find('自动战斗', (550, 155, 735, 205), exact=True)
        free = doc.find('仅使用免费活动AP', (400, 210, 710, 265), exact=True)
        cancel = doc.find('取消', (450, 510, 620, 580), exact=True)
        if (quick or auto) and free and cancel:
            identity = self.preparation.base.stage(doc, (550, 195, 745, 235))
            cost, submit = button_cost(doc, (590, 510, 800, 580))
            min_roi = (450, 370, 550, 420) if quick else (450, 290, 550, 330)
            minimum = doc.find('MIN', min_roi, exact=True)
            if not identity or not minimum or cost is None or not submit:
                return {'state': 'unknown', 'reason': 'event_dialog_controls_incomplete'}
            quantity = doc.extract_int(r'^战斗(\d+)次奖励$', (550, 265, 760, 335)) if quick else None
            if quick and quantity is None:
                return {'state': 'unknown', 'reason': 'quick_quantity_not_read'}
            points = {'cancel': cancel['point'], 'submit': submit,
                      'min': minimum['point'], 'free_toggle': [829, 253 if quick else 245],
                      'plus': [815, 441 if quick else 352],
                      'minus': [465, 441 if quick else 352]}
            # [身份/资源] 对话框核对本页标题；AP只来自统一固定区域数字，不依赖活动名称和图标。
            from event_generic_vision import title_identity
            title=title_identity(doc,(10,580,350,710))
            # [未知] 免费模式不依赖额外持有余额；未知额外余额保持None，不能当作0。
            return {'state': 'event_quick_dialog' if quick else 'event_auto_dialog', **identity, **(title or {}),
                    'total_cost': cost, 'quantity': quantity,
                    'free_only': DailyVision.toggle(image, (815, 242, 852, 264) if quick else (815, 235, 852, 255)),
                    'points': points, **balance}
        # The result is trusted only by the controller while a submitted event is pending.
        reward = doc.find('点击画面即可返回', (200, 500, 1120, 715))
        reward_heading = any(re.search(r'REWARD|获得\d+种', row['text'], re.I)
                             for row in doc.within((300, 50, 1000, 450)))
        if reward and reward_heading:
            return {'state': 'event_reward', 'points': {'dismiss': reward_dismiss_point()}, **balance}
        for text in ('战斗失败', 'DEFEAT', '败北'):
            if doc.find(text, (350, 80, 1000, 500), exact=True):
                return {'state': 'event_defeat', **balance}
        base = self.preparation.base.inspect(image, payload)
        # [层级] 主页只认固定字幕解锁；中心用实际标题及普通/挑战两个入口独立确认身份。
        from event_generic_vision import generic_event_observation
        generic=generic_event_observation(image,payload,base['state'])
        if generic:return generic
        if base['state'] == 'main_menu':
            entry = self.preparation.home_event(image, payload)
            if entry:
                base.update(points={'enter_event': entry['point']}, event_entry_evidence=entry)
                if entry.get('event_id'):base['event_id']=entry['event_id']
            return base
        if self.preparation.anchor(image, 'hub_loyal_maid'):
            normal = doc.find('普通战斗', (300, 600, 500, 680), exact=True)
            challenge = doc.find('挑战战斗', (490, 600, 640, 680), exact=True)
            if normal and challenge:
                return {'state': 'event_hub', 'event_id': 'Loyal_Maid_20261016',
                        'points': {'normal': normal['point'], 'challenge': challenge['point'], 'back': [113, 33]}, **balance}
        if base['state'] == 'event_preparation':
            cost, _ = button_cost(doc, (600, 620, 1200, 710))
            points = {'back': [113, 33]}
            quick_entry = doc.find('快速战斗', (400, 620, 900, 710), exact=True)
            auto_label = doc.find('自动战斗', (1080, 650, 1230, 710), exact=True)
            auto_switch = doc.find('OFF', (1080, 625, 1230, 680), exact=True) or doc.find('ON', (1080, 625, 1230, 680), exact=True)
            if quick_entry:
                points['open_quick'] = quick_entry['point']
            verified_auto = self.auto_entry(image) if auto_label and not quick_entry else None
            if auto_label and (auto_switch or verified_auto):
                points['open_auto'] = auto_switch['point'] if auto_switch else verified_auto
            # In the cleared layout the former auto coordinates overlap normal battle.
            # No automatic entry point is exposed without both actual labels.
            result = {**base, 'unit_cost': cost, 'points': points, **balance}
            if cost is None and (quick_entry or points.get('open_auto')):
                result['_unit_cost_roi'] = [1058, 650, 1073, 669] if quick_entry else [894, 650, 909, 669]
            return result
        if base['state'].startswith('event_'):
            base.update(balance)
        return base

    def inspect_file(self, path, output, payload=None):
        path, output = Path(path), Path(output)
        image = read_image(path)
        from common_navigation import PageDiagnosis
        if PageDiagnosis().inspect(image)['state']=='main_menu':
            from event_entry_vision import read_unlock_entry
            return read_unlock_entry(image,output/'home_entry') or {'state':'main_menu','points':{}}
        payload = payload if payload is not None else windows_ocr(path, output / 'ocr')
        result = self.inspect(image, payload)
        if result.get('state')=='unknown' or (result.get('state')=='main_menu' and not result.get('points',{}).get('enter_event')) or (result.get('state') in ('event_preparation','event_auto_dialog','event_quick_dialog') and not result.get('event_id')):
            from model_ocr_provider import recovery_page_ocr
            recovered=recovery_page_ocr(image)
            if recovered:
                candidate=self.inspect(image,recovered)
                if candidate.get('state')!='unknown':
                    result=candidate
                    result['ocr_recovery_backend']=recovered.get('backend')
        box = result.pop('_unit_cost_roi', None)
        if box:
            result['unit_cost'] = read_amount_crop(image, box, output, 'event_unit_cost')
            result['unit_cost_source'] = 'two_scale_fixed_crop_OCR' if result['unit_cost'] is not None else 'unread'
            if result['unit_cost'] is None:
                normalized = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)
                a, b, c, d = box
                region = normalized[b:d, a:c]
                values = {spec['value'] for spec, template in self.unit_templates
                          if spec['box'] == box and float(region.std()) > 8
                          and float(cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)[0, 0]) >= .985}
                if len(values) == 1:
                    result['unit_cost'] = values.pop()
                    result['unit_cost_source'] = 'exact_observed_unit_price_template'
        return result
