"""Current two-pass recognition, separated from input and journaling."""
import json
import re
import cv2
from common_navigation import PageDiagnosis
from daily_vision import OcrDocument, windows_ocr
from m00_recognize import read_image
from project_paths import ROOT


class PassRewardVision:
    def __init__(self):
        self.folder = ROOT / 'assets/pass_rewards'
        self.specs = json.loads((self.folder / 'sources.json').read_text(encoding='utf-8'))
        self.templates = {key: read_image(self.folder / spec['file']) for key, spec in self.specs.items()}

    def inspect(self, image, payload):
        if abs(image.shape[1] / image.shape[0] - 16 / 9) > .025:
            return {'state': 'unknown', 'reason': 'unsupported_aspect_ratio'}
        doc = OcrDocument(payload)
        if doc.find('点击画面即可返回', (400, 595, 900, 690)):
            return {'state': 'reward_overlay'}
        if not doc.find('通行证', (155, 15, 265, 55)):
            return {'state': PageDiagnosis().inspect(image)['state']}
        normalized = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY)
        if int((gray[21:49, 184:258] > 180).sum()) < 100:
            return {'state': 'unknown', 'reason': 'pass_title_dimmed_or_obscured'}
        scores = {}
        for key, spec in self.specs.items():
            x1, y1, x2, y2 = spec['box']
            scores[key] = float(cv2.matchTemplate(normalized[y1:y2, x1:x2], self.templates[key], cv2.TM_CCOEFF_NORMED)[0, 0])
        def best(prefix):
            ranked = sorted((k for k in scores if k.startswith(prefix)), key=scores.get, reverse=True)
            return ranked[0].split(':')[-1] if scores[ranked[0]] >= .97 and scores[ranked[0]] - scores[ranked[1]] >= .008 else None
        identity = best('identity:')
        tab = best('tab:' + identity + ':') if identity else None
        if identity is None or tab is None:
            return {'state': 'unknown', 'scores': scores}
        # The sole claim target is the aggregate reward button; premium/level purchase areas are excluded.
        button = gray[499:521, 950:993]
        bright, dark = float((button > 200).mean()), float((button < 165).mean())
        claimable = True if bright > .60 else False if dark > .85 else None
        current_level = doc.extract_int(r'(?i)LV\.?(\d+)', (380, 430, 500, 465))
        visible_levels = sorted({int(match.group(1)) for line in doc.within((620, 225, 690, 485))
                                 if (match := re.fullmatch(r'(?i)LV\.?(\d+)', line['text']))})
        return {'state': 'pass_' + tab, 'pass_id': identity, 'tab': tab, 'claimable': claimable,
                'current_level': current_level, 'visible_levels': visible_levels,
                'button_bright_fraction': bright, 'button_dark_fraction': dark, 'scores': scores}

    def inspect_file(self, path, output):
        return self.inspect(read_image(path), windows_ocr(path, output))
