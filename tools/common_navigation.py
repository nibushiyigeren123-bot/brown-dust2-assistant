"""Common, explicit return-to-menu diagnosis, independent of daily modules."""
import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np

from m00_recognize import OpeningRecognizer, ROOT, read_image
from ui_foreground import foreground_mask, masked_shape_match


class PageDiagnosis:
    def __init__(self):
        self.opening = OpeningRecognizer()
        self.assets = ROOT / "assets" / "navigation"
        self.config = json.loads((self.assets / "home_button.json").read_text(encoding="utf-8"))
        self.glyph = read_image(self.assets / self.config["template"])
        self.glyph_mask = foreground_mask(self.glyph)
        self.settings_glyph = read_image(self.assets / "settings_house.png")
        self.settings_mask = foreground_mask(self.settings_glyph)
        self.square_home_masks = None
        if (self.assets / 'square_home_glyph.png').exists() and (self.assets / 'square_home_key.png').exists():
            self.square_home_masks = [foreground_mask(read_image(self.assets / name))
                for name in ('square_home_glyph.png', 'square_home_key.png')]

    def _find_glyph(self, image, glyph_mask, source_width, threshold):
        height, width = image.shape[:2]
        if abs(width / height - 16 / 9) > 0.025:
            return None
        norm_width = self.config["normalized_width"]
        normalized = cv2.resize(image, (norm_width, 720), interpolation=cv2.INTER_AREA)
        x1, y1, x2, y2 = self.config["search_roi_normalized"]
        ox, oy = round(x1 * norm_width), round(y1 * 720)
        region = normalized[oy:round(y2 * 720), ox:round(x2 * norm_width)]
        region_gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        base = norm_width / source_width
        best = None
        for factor in [0.8, 0.9, 1.0, 1.1, 1.2]:
            gray = cv2.resize(glyph_mask, None, fx=base * factor, fy=base * factor,
                              interpolation=cv2.INTER_NEAREST)
            matched = masked_shape_match(region_gray, gray)
            score, position = matched["score"], matched["position"]
            if best is None or score > best["score"]:
                tx, ty = position
                th, tw = gray.shape
                best = {"score": float(score), "box_normalized_pixels": [ox + tx, oy + ty, tw, th]}
        if best["score"] < threshold:
            return None
        x, y, w, h = best["box_normalized_pixels"]
        return {"score": round(best["score"], 4),
                "client_point": [(x + w / 2) * width / norm_width, (y + h / 2) * height / 720],
                "client_box": [x * width / norm_width, y * height / 720,
                               w * width / norm_width, h * height / 720]}

    def find_home(self, image):
        # A house-with-gear opens wallpaper settings; it is never a return button.
        if self.find_settings(image):
            return None
        original = self._find_glyph(image, self.glyph_mask, self.config["source_client_width"], 0.70)
        if original:
            return original
        if self.square_home_masks:
            house, key = [self._find_glyph(image, mask, 1280, 0.70) for mask in self.square_home_masks]
            if house and key:
                dx = (key['client_point'][0] - house['client_point'][0]) * 1280 / image.shape[1]
                dy = (key['client_point'][1] - house['client_point'][1]) * 720 / image.shape[0]
                if abs(dx) <= 6 and 12 <= dy <= 32:
                    return {**house, 'variant': 'square_house_and_H_key'}
        return None

    def find_settings(self, image):
        return self._find_glyph(image, self.settings_mask, 1668, 0.70)

    def inspect(self, image):
        opening = self.opening.recognize(image)
        if opening["state"] == "main_menu":
            return {"state": "main_menu", "next_action": "done", "opening": opening}
        if opening["next_action"] == "click_blank":
            return {"state": opening["state"], "next_action": "dismiss_opening", "opening": opening}
        settings = self.find_settings(image)
        if settings:
            return {"state": "settings_house_present", "next_action": "wait",
                    "reason": "house_with_gear_is_not_return", "settings_button": settings, "opening": opening}
        home = self.find_home(image)
        if home:
            return {"state": "scene_with_home", "next_action": "click_home", "home_button": home}
        return {"state": "unknown", "next_action": "wait", "opening": opening}


def ensure_main_menu(window, output, execute=False, timeout=20):
    """Called when a module needs the menu; not a permanent background loop."""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "run.json"
    diagnosis = PageDiagnosis()
    report = {"execute": execute, "completed": False, "actions": [], "observations": []}
    deadline = time.monotonic() + timeout
    home_attempts = 0
    last_input = 0.0

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        while time.monotonic() < deadline:
            # F8 is a local stop key while this short operation is running.
            import win32api
            if win32api.GetAsyncKeyState(0x77) & 0x8000:
                report["stopped"] = "F8"
                break
            image, geometry = window.capture()
            frame = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
            result = diagnosis.inspect(frame)
            index = len(report["observations"])
            path = output / f"frame_{index:03}.png"
            image.save(path)
            report["observations"].append({"image": str(path), "window": geometry, **result})
            save()
            if result["state"] == "main_menu":
                report["completed"] = True
                report["completion"] = "main_menu_observed"
                break
            if not execute:
                break
            if time.monotonic() - last_input >= max(.8, min(15, timeout / 8)):
                if result["next_action"] == "click_home" and home_attempts < 3:
                    point = result["home_button"]["client_point"]
                    event = window.click_client(point, geometry)
                    report["actions"].append({"type": "click_home", **event})
                    home_attempts += 1
                    last_input = time.monotonic()
                elif result["next_action"] == "dismiss_opening":
                    nx, ny = result["opening"]["suggested_point_normalized"]
                    event = window.click_client((nx * geometry["width"], ny * geometry["height"]), geometry)
                    report["actions"].append({"type": "click_blank", **event})
                    last_input = time.monotonic()
            save()
            time.sleep(0.3)
        if execute and not report["completed"] and "stopped" not in report:
            report["stopped"] = "menu_not_verified_within_timeout"
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, help="Inspect a saved client screenshot; never clicks")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="Return the live game to its menu")
    args = parser.parse_args()
    if args.image:
        if args.execute:
            parser.error("--execute applies only to the live game")
        result = PageDiagnosis().inspect(read_image(args.image.resolve(strict=True)))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
    else:
        from game_window import GameWindow
        from runtime_policy import network_wait_seconds
        report = ensure_main_menu(GameWindow.find(), args.output, execute=args.execute, timeout=network_wait_seconds())
        print(json.dumps({"completed": report["completed"], "actions": report["actions"],
                          "last_state": report["observations"][-1]["state"] if report["observations"] else None}, ensure_ascii=False))
        if args.execute and not report["completed"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
