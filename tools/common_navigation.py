"""Common, explicit return-to-menu diagnosis, independent of daily modules."""
from reward_overlay_vision import reward_dismiss_point
from frame_store import next_path, report_write, persist_references
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
        from reward_overlay_vision import RewardOverlayVision
        self.reward_overlay = RewardOverlayVision()
        self.opening = OpeningRecognizer()
        from return_navigation_vision import ReturnNavigationVision
        self.returns = ReturnNavigationVision()
        self.assets = ROOT / "assets" / "navigation"
        self.config = json.loads((self.assets / "home_button.json").read_text(encoding="utf-8"))
        source_width = self.config.get('source_client_width')
        if type(source_width) not in (int, float) or not np.isfinite(source_width) or source_width <= 0:
            raise ValueError('导航资源配置缺少有效的 source_client_width，请完整解压新版程序。')
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
        overlay = self.reward_overlay.inspect(image)
        if overlay:
            h, w = image.shape[:2]
            return {**overlay, 'next_action': 'close_panel', 'return_button': {
                'client_point': reward_dismiss_point(w, h),
                'normalized_point': reward_dismiss_point(), 'scores': overlay['visual_scores']}}
        opening = self.opening.recognize(image)
        # Known modal exits precede generic opening dismissal. Their independent
        # title and button patches prevent a dim backdrop authorizing a click.
        returned = self.returns.inspect(image)
        if returned and returned['next_action'] in ('cancel_panel','close_panel'):
            return {**returned, 'opening':opening}
        if opening["state"] == "main_menu":
            return {"state": "main_menu", "next_action": "done", "opening": opening}
        if opening['state'] == 'loading_or_unknown_overlay':
            return {'state':opening['state'],'next_action':'wait','opening':opening}
        if returned:
            return {**returned, 'opening':opening}
        if opening["next_action"] == "click_blank":
            return {"state": opening["state"], "next_action": "dismiss_opening", "opening": opening}
        if opening['state'] == 'pass_panel':
            return {'state':opening['state'],'next_action':'wait','opening':opening}
        settings = self.find_settings(image)
        if settings:
            return {"state": "settings_house_present", "next_action": "wait",
                    "reason": "house_with_gear_is_not_return", "settings_button": settings, "opening": opening}
        home = self.find_home(image)
        if home:
            return {"state": "scene_with_home", "next_action": "click_home", "home_button": home}
        return {"state": "unknown", "next_action": "wait", "opening": opening}


def ensure_main_menu(window, output, execute=False, timeout=20):
    """Observe every input; opening ads continue until the confirmed main menu."""
    from game_window import check_requested_stop
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    diagnosis = PageDiagnosis()
    from queue_recognition import context_for_goal
    context = context_for_goal()
    report = {"execute": execute, "completed": False, "actions": [], "observations": []}
    deadline = time.monotonic() + timeout
    previous = None
    stable = home_stable = attempts = 0
    attempts_by_control = {}
    recovered = False
    last_input = 0.0
    opening_session = False
    non_opening_actions = 0
    clickable = ('click_home','dismiss_opening','click_back','close_panel','cancel_panel')

    def save():
        report_write(output/'run.json', report)

    def capture():
        check_requested_stop()
        image, geometry = window.capture()
        frame = cv2.cvtColor(np.asarray(image),cv2.COLOR_RGB2BGR)
        started=time.monotonic();result=context.diagnose(frame, diagnosis.inspect(frame))
        result['recognition_ms']=round((time.monotonic()-started)*1000,1)
        path=output/f"frame_{len(report['observations']):03d}.png";path=next_path(path);image.save(path)
        report['observations'].append({'image':str(path),'window':geometry,**result});save()
        return result,geometry,frame

    def target(result, geometry, attempt=0):
        if result['state'] == 'reward_overlay':
            return reward_dismiss_point(geometry['width'], geometry['height'], attempt)
        if result['next_action']=='click_home':return result['home_button']['client_point']
        if result['next_action']=='dismiss_opening':
            x,y=result['opening']['suggested_point_normalized']
            return [x*geometry['width'],y*geometry['height']]
        return result['return_button']['client_point']

    try:
        while opening_session or time.monotonic()<deadline:
            result,geometry,frame=capture()
            if result['state']=='main_menu':
                home_stable+=1
                if not execute or home_stable>=2:
                    report.update(completed=True,completion='main_menu_observed_twice' if execute else 'main_menu_observed')
                    break
                time.sleep(.25);continue
            home_stable=0
            if not execute:break
            if result['next_action'] == 'dismiss_opening':
                opening_session = True
            if result['next_action'] not in clickable:
                stable=0;previous=None;time.sleep(.2);continue
            point=target(result,geometry)
            signature=(result['state'],result['next_action'],tuple(round(v) for v in point),geometry)
            key=(result['state'],result['next_action'],tuple(round(v) for v in point))
            same=signature==previous
            stable=stable+1 if same else 1
            attempts=attempts_by_control.get(key,0)
            previous=signature
            if stable<2 or time.monotonic()-last_input<.8:
                time.sleep(.2);continue
            is_opening = result['next_action'] == 'dismiss_opening'
            if not is_opening and attempts>=3:
                report['stopped']='同一返回入口尝试3次仍未确认离开；已停止，不继续盲点。';break
            if not is_opening and non_opening_actions>=12:
                report['stopped']='返回导航已达本轮动作上限，停止核对。';break
            if not is_opening and attempts==2 and not recovered:
                event=window.recover_input(geometry)
                report['actions'].append({'type':'recover_window_input',**event});save()
                recovered=True;stable=0;last_input=time.monotonic();continue
            point=target(result,geometry,attempts)
            window.focus_client(geometry)
            verified,fresh_geometry,_=capture()
            if fresh_geometry!=geometry or (verified['state'],verified['next_action'])!=(result['state'],result['next_action']):
                previous=None;continue
            fresh_point=target(verified,fresh_geometry,attempts)
            if max(abs(a-b) for a,b in zip(point,fresh_point))>5:
                previous=None;continue
            click_options = {'native_motion': True} if verified['state'] == 'reward_overlay' else {}
            event=window.click_client(fresh_point,fresh_geometry,press_ms=250,**click_options)
            if not is_opening:
                non_opening_actions += 1
            attempts+=1;attempts_by_control[key]=attempts;last_input=time.monotonic()
            report['actions'].append({'type':verified['next_action'],'source_state':verified['state'],'attempt':attempts,**event});save()
            time.sleep(.4)
        if execute and not report['completed'] and 'stopped' not in report:
            report['stopped']='menu_not_verified_within_timeout'
    except Exception as error:
        report['error']=str(error)
        raise
    finally:save()
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
