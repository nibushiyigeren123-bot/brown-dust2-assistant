"""Recognize the universal reward modal from existing shared image anchors.

No OCR phrase, screen-wide embedding, account block or inferred click is used.
"""
from functools import lru_cache
import hashlib
import json
import cv2
import numpy as np
from project_paths import ROOT


@lru_cache(maxsize=1)
def reward_anchors():
    folder = ROOT / 'assets/shared_references'
    spec = json.loads((folder / 'index.json').read_text(encoding='utf-8'))['templates']['reward_overlay']
    raw = (folder / spec['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != spec['sha256']:
        raise ValueError('Reward reference hash changed')
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None or image.shape != (720, 1280):
        raise ValueError('Invalid full-client reward reference')
    return image[209:239, 585:695].copy(), image[651:669, 579:697].copy()


class RewardOverlayVision:
    def inspect(self, image):
        height, width = image.shape[:2]
        if width < 640 or height < 360 or abs(width / height - 16 / 9) > .025:
            return None
        norm = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)
        # The left and right parts of the modal band exclude reward items.
        blue = []
        for left, right in ((0, 400), (880, 1280)):
            b, g, r = cv2.split(norm[300:420, left:right].astype(np.float32))
            blue.append(float(((b - r > 30) & (b - g > 15) & (b > 60)).mean()))
        if min(blue) < .85:
            return None
        gray = cv2.cvtColor(norm, cv2.COLOR_BGR2GRAY)
        title, hint = reward_anchors()
        scores = {}
        for name, template, region, light in (
                ('reward_title', title, gray[190:255, 550:730], 155),
                ('return_hint', hint, gray[635:685, 530:750], 95)):
            matched = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
            _, score, _, (x, y) = cv2.minMaxLoc(matched)
            if not np.isfinite(score) or score < .94:
                return None
            patch = region[y:y + template.shape[0], x:x + template.shape[1]]
            ink = template >= 120
            if not ink.any() or float(patch[ink].mean()) < light:
                return None
            scores[name] = round(float(score), 4)
        scores.update(blue_left=round(blue[0], 4), blue_right=round(blue[1], 4))
        return {'state': 'reward_overlay', 'recognition_basis': 'shared_reward_title_return_hint_and_blue_band',
            'visual_scores': scores}
