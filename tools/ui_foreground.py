"""Match UI foreground shapes while excluding wallpaper pixels from templates."""
import json
from pathlib import Path

import cv2
import numpy as np

from project_paths import ROOT


def foreground_mask(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    return mask


def masked_shape_match(gray_region, glyph):
    """Only the glyph and its one-pixel contour neighborhood affect the score."""
    mask = foreground_mask(glyph) if glyph.ndim == 3 else glyph
    support = cv2.dilate(mask, np.ones((3, 3), dtype=np.uint8), iterations=1)
    values = cv2.matchTemplate(gray_region, mask, cv2.TM_CCOEFF_NORMED, mask=support)
    # Flat regions have zero variance and cannot establish an icon match.
    values[~np.isfinite(values)] = -1
    _, score, _, point = cv2.minMaxLoc(values)
    x, y = point
    sample = gray_region[y:y + mask.shape[0], x:x + mask.shape[1]]
    return {"score": float(score), "position": point,
            "foreground_gray": float(sample[mask > 0].mean())}


class MenuForegroundRecognizer:
    def __init__(self):
        assets = ROOT / "assets" / "ui_foreground"
        config = json.loads((assets / "config.json").read_text(encoding="utf-8"))
        self.anchors = []
        for item in config["anchors"]:
            mask = cv2.imdecode(np.fromfile(str(assets / item["file"]), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            self.anchors.append((item, mask))

    def inspect(self, image):
        height, width = image.shape[:2]
        if abs(width / height - 16 / 9) > 0.025:
            return {"state": "unknown", "reason": "unsupported_aspect_ratio", "anchors": {}}
        gray = cv2.cvtColor(cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        scores = {}
        for item, mask in self.anchors:
            a, b, c, d = item["box_1280"]
            region = gray[b - 5:d + 5, a - 5:c + 5]
            result = masked_shape_match(region, mask)
            scores[item["id"]] = {"shape_score": round(result["score"], 4),
                                   "foreground_gray": round(result["foreground_gray"], 2)}
        shapes_match = all(s["shape_score"] >= 0.70 for s in scores.values())
        if shapes_match and all(s["foreground_gray"] >= 120 for s in scores.values()):
            state = "main_menu"
        elif shapes_match and all(15 <= s["foreground_gray"] <= 110 for s in scores.values()):
            state = "menu_overlay"
        else:
            state = "unknown"
        return {"state": state, "anchors": scores, "method": "ui_foreground_masks"}
