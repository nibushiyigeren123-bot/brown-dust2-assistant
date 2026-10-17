"""Local equipment UI recognition. Does not send game input."""
import hashlib
import json
import re
from pathlib import Path

import cv2

from daily_vision import OcrDocument, compact, windows_ocr
from m00_recognize import ROOT, read_image


def read_inventory_count(image, output):
    image = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)
    crop = image[25:48, 235:314]
    crop = cv2.copyMakeBorder(crop, 6, 6, 6, 6, cv2.BORDER_CONSTANT, value=0)
    readings, values = [], []
    for scale in (3, 4):
        encoded = cv2.imencode('.png', cv2.resize(crop, None, fx=scale, fy=scale,
                                                interpolation=cv2.INTER_CUBIC))[1]
        path = Path(output) / ('inventory_' + hashlib.sha256(encoded.tobytes()).hexdigest()[:16] + '.png')
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded.tofile(str(path))
        payload = windows_ocr(path, Path(output) / 'ocr', language='en-US')
        text = compact(''.join(line['text'] for line in payload['lines']))
        readings.append(text)
        match = re.fullmatch(r'(\d{1,4})/2000', text)
        values.append(int(match[1]) if match else None)
    return {'value': values[0] if values[0] is not None and values[0] == values[1] else None,
            'readings': readings}


def read_integer(image, box, output, label):
    """Read the same numeric field at two sizes, accepting only grouped integers."""
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
            encoded.tofile(str(path))
        payload = windows_ocr(path, Path(output) / 'ocr', language='en-US')
        text = compact(''.join(line['text'] for line in payload['lines']))
        readings.append(text)
        valid = re.fullmatch(r'(?:\d{1,3}(?:[,.]\d{3})+|\d+)', text)
        values.append(int(text.replace(',', '').replace('.', '')) if valid else None)
    return {'value': values[0] if values[0] is not None and values[0] == values[1] else None,
            'readings': readings}


class EquipmentVision:
    def __init__(self):
        assets = ROOT / 'assets/equipment'
        self.specs = json.loads((assets / 'sources.json').read_text(encoding='utf-8'))
        self.images = {name: read_image(assets / spec['file']) for name, spec in self.specs.items()}

    def anchor(self, image, name, threshold=.88):
        image = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)
        a, b, c, d = self.specs[name]['box']
        region = image[max(0, b-3):min(720, d+3), max(0, a-3):min(1280, c+3)]
        score = float(cv2.matchTemplate(region, self.images[name], cv2.TM_CCOEFF_NORMED).max())
        return score >= threshold

    def inspect_file(self, path, output):
        image = read_image(path)
        doc = OcrDocument(windows_ocr(path, Path(output) / 'ocr'))
        return self.inspect(image, doc, output)

    def inspect(self, image, doc, output):
        if abs(image.shape[1] / image.shape[0] - 16/9) > .035:
            return {'state': 'unknown', 'reason': 'unsupported_aspect_ratio'}
        if 'decompose_result_title' in self.images and self.anchor(image, 'decompose_result_title', .97):
            return {'state': 'equipment_decompose_result',
                    'matches_reviewed_55_5_result': self.anchor(image, 'decompose_result_rewards', .97)}
        if 'filter_title' in self.images and self.anchor(image, 'filter_title'):
            selected = ['R'] if self.anchor(image, 'filter_r_only', .97) else (
                [] if self.anchor(image, 'filter_quality_default', .97) else None)
            return {'state': 'equipment_filter', 'selected_qualities': selected,
                    'worn_hidden': self.anchor(image, 'filter_worn_off', .97),
                    'default_visible_options': self.anchor(image, 'filter_quality_default', .97)
                        and self.anchor(image, 'filter_other_default', .97)
                        and self.anchor(image, 'filter_worn_default', .97),
                    'saved_toast': self.anchor(image, 'filter_saved_toast', .97)}
        if 'decompose_confirm_warning' in self.images and self.anchor(image, 'decompose_confirm_warning', .97):
            return {'state': 'equipment_decompose_confirmation',
                    'matches_reviewed_one_item_confirmation': self.anchor(image, 'decompose_confirm_one', .97)}
        if 'decompose_select_prompt' in self.images and self.anchor(image, 'decompose_select_prompt'):
            return {'state': 'equipment_decompose_selection',
                    'matches_reviewed_one_item_selection': self.anchor(image, 'selected_count_one', .97)}
        if 'detail_r_gun' in self.images and self.anchor(image, 'detail_r_gun', .97):
            return {'state': 'equipment_reviewed_R_detail', 'quality': 'R',
                    'unlocked': self.anchor(image, 'detail_unlocked', .97),
                    'not_equipped': self.anchor(image, 'detail_wear_available', .97)}
        if ('refine_title' in self.images and self.anchor(image, 'refine_single')
                and (self.anchor(image, 'refine_title') or
                     (self.anchor(image, 'refine_score_label') and self.anchor(image, 'refine_material_label')))):
            fields = {name: read_integer(image, box, output, name) for name, box in {
                'material_balance': (989, 25, 1068, 49), 'gold_balance': (1099, 25, 1200, 49),
                'material_cost': (1020, 592, 1048, 617), 'gold_cost': (1088, 658, 1118, 681),
            }.items()}
            score = 20 if self.anchor(image, 'refine_score20', threshold=.97) else None
            return {'state': 'equipment_refine', 'score': score, 'numeric_evidence': fields,
                    'before': {'material': fields['material_balance']['value'], 'gold': fields['gold_balance']['value']},
                    'cost': {'material': fields['material_cost']['value'], 'gold': fields['gold_cost']['value']},
                    'points': {'submit': [1070, 670], 'back': [113, 33]}}
        if self.anchor(image, 'grid_title', .97):
            return {'state': 'equipment_grid', 'inventory': read_inventory_count(image, output)}
        from ui_foreground import MenuForegroundRecognizer
        if MenuForegroundRecognizer().inspect(image)['state'] == 'main_menu':
            return {'state': 'main_menu'}
        return {'state': 'unknown'}
