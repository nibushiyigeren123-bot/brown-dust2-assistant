"""Recognize the observed daily/weekly UI; never sends input."""
import json
import re
import cv2
from reward_overlay_vision import RewardOverlayVision
from daily_vision import OcrDocument, windows_ocr
from common_navigation import PageDiagnosis
from m00_recognize import ROOT, read_image


class TaskRewardVision:
    def __init__(self):
        self.reward_overlay = RewardOverlayVision()
        self.folder = ROOT / 'assets/task_rewards'
        self.specs = json.loads((self.folder / 'sources.json').read_text(encoding='utf-8'))
        self.templates = {k: cv2.cvtColor(read_image(self.folder / v['file']), cv2.COLOR_BGR2GRAY)
                          for k, v in self.specs.items()}

    def inspect(self, image, payload):
        if abs(image.shape[1] / image.shape[0] - 16/9) > .025:
            return {'state': 'unknown', 'reason': 'unsupported_aspect_ratio'}
        overlay = self.reward_overlay.inspect(image)
        if overlay:
            return overlay
        doc = OcrDocument(payload)
        if doc.find('点击画面即可返回', (400, 595, 900, 690)):
            return {'state': 'reward_overlay'}
        normalized = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY)
        scores = {}
        for name, spec in self.specs.items():
            a, b, c, d = spec['box']
            scores[name] = float(cv2.matchTemplate(gray[b:d, a:c], self.templates[name], cv2.TM_CCOEFF_NORMED)[0, 0])
        ranked = sorted(scores, key=scores.get, reverse=True)
        mode = ranked[0]
        title_lit = int((gray[21:49, 184:276] > 180).sum()) >= 100
        if title_lit and scores[mode] >= .96 and scores[mode] - scores[ranked[1]] >= .015 and doc.find('区间奖励', (370, 15, 480, 55)):
            fill = gray[660:681, 1080:1180]
            bright_fraction = float((fill > 170).mean())
            dark_fraction = float((fill < 135).mean())
            claimable = True if bright_fraction > .65 else False if dark_fraction > .9 else None
            rows = []
            for line in doc.within((450, 85, 950, 650)):
                match = re.search(r'(.+?)(\d+)/(\d+)$', line['text'])
                if match:
                    rows.append({'name': match[1], 'current': int(match[2]), 'target': int(match[3])})
            return {'state': mode + '_tasks', 'mode': mode, 'claimable': claimable,
                    'button_bright_fraction': bright_fraction, 'button_dark_fraction': dark_fraction,
                    'title_scores': scores, 'rows': rows,
                    'incomplete_visible': [r for r in rows if r['current'] < r['target']]}
        if PageDiagnosis().inspect(image)['state'] == 'main_menu':
            return {'state': 'main_menu'}
        return {'state': 'unknown', 'title_scores': scores}

    def inspect_file(self, path, output):
        image = read_image(path)
        overlay = self.reward_overlay.inspect(image)
        if overlay:
            return overlay
        return self.inspect(image, windows_ocr(path, output))
