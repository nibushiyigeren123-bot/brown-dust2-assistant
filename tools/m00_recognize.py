"""Read-only M00 prototype: classify recorded frames and suggest the next step.

This module sends no mouse or keyboard input. It recognizes menu foreground,
known opening dialogs and dimmed menu overlays; other pages remain unknown.
"""
import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from ui_foreground import MenuForegroundRecognizer

from project_paths import ROOT


def read_image(path):
    from frame_store import image_bytes
    image = cv2.imdecode(np.frombuffer(image_bytes(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Cannot decode image: {path}")
    return image


class OpeningRecognizer:
    def __init__(self, assets=None):
        self.assets = Path(assets) if assets else ROOT / "assets" / "m00"
        self.config = json.loads((self.assets / "templates.json").read_text(encoding="utf-8"))
        self.templates = {item["id"]: read_image(self.assets / item["file"])
                          for item in self.config["regions"] if not item["id"].startswith("home_")}
        self.menu = MenuForegroundRecognizer()
        from panel_identity import PassPanelSignature
        self.pass_panel = PassPanelSignature()

    @staticmethod
    def large_center_panel(normalized):
        """Layout is supporting evidence only; dark backdrop alone authorizes nothing."""
        gray=cv2.cvtColor(normalized,cv2.COLOR_BGR2GRAY)
        center=gray[100:615,180:1100]
        surround=np.concatenate((gray[100:615,0:100].ravel(),gray[100:615,1180:1280].ravel()))
        bright=(center>100).astype(np.uint8)
        points=cv2.findNonZero(bright)
        if points is None:return False
        _,_,width,height=cv2.boundingRect(points)
        return bool(bright.mean()>.18 and center.mean()-surround.mean()>20 and width>500 and height>230)

    def recognize(self, image):
        height, width = image.shape[:2]
        if abs(width / height - 16 / 9) > 0.025:
            return {"state": "unknown", "next_action": "pause", "reason": "unsupported_aspect_ratio"}
        normalized = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)
        menu = self.menu.inspect(image)
        if menu["state"] == "main_menu":
            return {"state": "main_menu", "next_action": "finish_m00", "ui": menu}
        scores = {}
        for item in self.config["regions"]:
            if item["id"].startswith("home_"):
                continue  # Legacy wide patches included wallpaper; no longer used.
            x1, y1, x2, y2 = item["box_1280"]
            patch = normalized[y1:y2, x1:x2]
            template = self.templates[item["id"]]
            gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
            reference = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
            correlation = float(cv2.matchTemplate(gray, reference, cv2.TM_CCOEFF_NORMED)[0, 0])
            luminance_ratio = float(gray.mean() / max(reference.mean(), 1.0))
            scores[item["id"]] = {"correlation": round(correlation, 4),
                                    "luminance_ratio": round(luminance_ratio, 4)}
        for name in ["signin_event", "signin_pass"]:
            if scores[name]["correlation"] >= 0.86:
                return {"state": name, "next_action": "click_blank",
                        "suggested_point_normalized": [0.04, 0.50], "scores": scores, "ui": menu}
        # A pass has the same central layout as an opening advertisement.
        # Its independent identity AND active-tab signature prevent blank clicks.
        panel = self.pass_panel.identify(normalized)
        if panel:
            return {'state':'pass_panel','next_action':'pause','panel':panel,'ui':menu}
        if menu["state"] == "menu_overlay":
            if self.large_center_panel(normalized):
                return {"state": "menu_overlay", "next_action": "click_blank",
                        "suggested_point_normalized": [0.04, 0.50], "ui": menu,
                        "layout": "large_center_panel_with_dimmed_menu"}
            return {"state":"loading_or_unknown_overlay","next_action":"pause","ui":menu,
                    "reason":"dimmed_menu_without_verified_large_panel_wait_without_input"}
        return {"state": "unknown", "next_action": "pause", "scores": scores, "ui": menu}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="A screenshot or a folder of recorded JPG frames")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    source = args.input.resolve(strict=True)
    paths = sorted(source.glob("*.jpg")) if source.is_dir() else [source]
    if not paths:
        parser.error("No JPG frames found")
    recognizer = OpeningRecognizer()
    results = [{"image": str(path), **recognizer.recognize(read_image(path))} for path in paths]
    document = {"mode": "read_only", "input_actions_sent": 0, "results": results}
    if args.output:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    counts = {}
    for result in results:
        counts[result["state"]] = counts.get(result["state"], 0) + 1
    print(json.dumps({"mode": "read_only", "input_actions_sent": 0,
                      "images": len(results), "states": counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
