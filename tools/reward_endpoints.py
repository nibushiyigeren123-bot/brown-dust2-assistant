"""Exact reviewed current-list endpoints; static unrecognized middle pages never pass."""
import json
import cv2
from m00_recognize import read_image
from project_paths import ROOT


class RewardEndpoints:
    def __init__(self):
        folder = ROOT / 'assets/reward_endpoints'
        self.specs = json.loads((folder / 'sources.json').read_text(encoding='utf-8'))
        self.templates = {key: cv2.cvtColor(read_image(folder / spec['file']), cv2.COLOR_BGR2GRAY) for key, spec in self.specs.items()}

    def confirmed(self, image, key):
        if key not in self.specs or abs(image.shape[1] / image.shape[0] - 16 / 9) > .025:
            return False
        normalized = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY)
        for name, spec in self.specs.items():
            if name != key and not name.startswith(key + ':'):
                continue
            x1, y1, x2, y2 = spec['box']
            view = gray[y1:y2, x1:x2]
            # Claiming can reorder a list. Only separately reviewed variants
            # are accepted; never lower the threshold to fit an unknown page.
            template = self.templates[name]
            if float(cv2.matchTemplate(view, template, cv2.TM_CCOEFF_NORMED)[0, 0]) >= .97 and float(cv2.absdiff(view, template).mean()) < 8:
                return True
        return False
