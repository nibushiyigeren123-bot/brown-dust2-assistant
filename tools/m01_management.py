"""M01: open the cycling management entry and collect only an enabled button."""
from frame_store import next_path, report_write, persist_references
import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np

from common_navigation import PageDiagnosis, ensure_main_menu
from m00_recognize import ROOT, read_image
from ui_foreground import foreground_mask, masked_shape_match


class ManagementRecognizer:
    def __init__(self):
        self.assets = ROOT / "assets" / "management"
        self.config = json.loads((self.assets / "config.json").read_text(encoding="utf-8"))
        self.entry = read_image(self.assets / self.config["entry_template"])
        self.title = read_image(self.assets / self.config["title_template"])
        self.reward = read_image(self.assets / "reward_word.png")

    @staticmethod
    def _match(image, template, roi, source_width=1668, threshold=0.72, foreground_only=False):
        h, w = image.shape[:2]
        if abs(w / h - 16 / 9) > 0.035:
            return None
        normalized = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)
        left, top, right, bottom = [round(v * dimension) for v, dimension in zip(roi, [1280, 720, 1280, 720])]
        region = cv2.cvtColor(normalized[top:bottom, left:right], cv2.COLOR_BGR2GRAY)
        best = None
        base = 1280 / source_width
        for factor in [0.88, 0.92, 0.96, 1.0, 1.04, 1.08, 1.12]:
            patch = cv2.resize(template, None, fx=base * factor, fy=base * factor, interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
            if foreground_only:
                matched = masked_shape_match(region, foreground_mask(gray))
                score, position = matched["score"], matched["position"]
            else:
                values = cv2.matchTemplate(region, gray, cv2.TM_CCOEFF_NORMED)
                _, score, _, position = cv2.minMaxLoc(values)
            if best is None or score > best["score"]:
                best = {"score": float(score), "x": left + position[0], "y": top + position[1],
                        "width": patch.shape[1], "height": patch.shape[0],
                        "scale": patch.shape[1] / template.shape[1]}
        return best if best["score"] >= threshold else None

    def entry_point(self, image, *, main_menu_verified=False):
        # Fishing/restaurant/assistant rotate inside the same home-menu slot.
        # Once the foreground home page is verified, click its fixed icon centre;
        # don't derive a moving point from the currently visible icon or label.
        h, w = image.shape[:2]
        if abs(w / h - 16 / 9) > 0.035:
            return None
        match = None if main_menu_verified else self._match(
            image, self.entry, (0.035, 0.16, 0.18, 0.34), threshold=0.67, foreground_only=True)
        if not main_menu_verified and match is None:
            return None
        return {"score": round(match["score"], 4) if match else None,
                "method": "fixed_management_slot", "home_verified": main_menu_verified,
                "client_point": [110 * w / 1280, 170 * h / 720]}

    def panel(self, image):
        match = self._match(image, self.title, (0.30, 0.12, 0.72, 0.42), threshold=0.75)
        if match is None:
            return None
        h, w = image.shape[:2]
        normalized = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)
        tx, ty, _, _ = self.config["title_box"]
        scale = match["scale"]
        left, top = match["x"] - tx * scale, match["y"] - ty * scale

        def button(box):
            x1, y1, x2, y2 = box
            pixels = [round(left + x1 * scale), round(top + y1 * scale),
                      round(left + x2 * scale), round(top + y2 * scale)]
            a, b, c, d = pixels
            if a < 0 or b < 0 or c > 1280 or d > 720:
                raise ValueError("Detected panel buttons extend outside screenshot")
            inset_x, inset_y = max(2, round(12 * scale)), max(2, round(7 * scale))
            patch = normalized[b + inset_y:d - inset_y, a + inset_x:c - inset_x]
            median = float(np.median(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)))
            return {"median_gray": median,
                    "client_point": [(a + c) * w / 2560, (b + d) * h / 1440],
                    "client_box": [a * w / 1280, b * h / 720, (c - a) * w / 1280, (d - b) * h / 720]}

        cancel = button(self.config["cancel_box"])
        collect = button(self.config["collect_box"])
        ratio = collect["median_gray"] / max(cancel["median_gray"], 1)
        state = ("enabled" if ratio >= self.config["enabled_ratio_min"]
                 else "disabled" if ratio <= self.config["disabled_ratio_max"] else "uncertain")
        return {"state": state, "title_score": round(match["score"], 4),
                "brightness_ratio": round(ratio, 4), "collect": collect, "cancel": cancel}

    def inspect(self, image):
        reward = self._match(image, self.reward, (0.34, 0.20, 0.67, 0.48), source_width=2560, threshold=0.72, foreground_only=True)
        if reward:
            return {"state": "reward", "score": round(reward["score"], 4),
                    "management_context": self.panel(image) is not None}
        panel = self.panel(image)
        if panel:
            return {"state": "management_panel", "panel": panel}
        diagnosis = PageDiagnosis().inspect(image)
        if diagnosis["state"] == "main_menu":
            return {"state": "main_menu", "entry": self.entry_point(image, main_menu_verified=True)}
        return {"state": "other_page", "diagnosis": diagnosis}


def run_management(window, output, execute=False, timeout=20):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {"execute": execute, "completed": False, "outcome": None,
              "actions": [], "observations": []}
    recognizer = ManagementRecognizer()
    collect_clicked = False
    entry_attempts = 0
    entry_recovered = False
    last_action_kind = None
    closing = False
    close_attempts = 0
    last_action = 0.0
    deadline = time.monotonic() + timeout

    def save():
        report_write(output / "run.json", report)

    try:
        initial_image, _ = window.capture()
        initial = recognizer.inspect(cv2.cvtColor(np.asarray(initial_image), cv2.COLOR_RGB2BGR))
        existing_reward = initial["state"] == "reward" and initial.get("management_context", False)
        if execute and initial["state"] not in {"management_panel", "main_menu"} and not existing_reward:
            navigation = ensure_main_menu(window, output / "navigation", execute=True, timeout=timeout)
            if not navigation["completed"]:
                raise RuntimeError("Could not establish main menu before management")
            deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            import win32api
            if win32api.GetAsyncKeyState(0x77) & 0x8000:
                report["stopped"] = "F8"
                break
            image, geometry = window.capture()
            result = recognizer.inspect(cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR))
            path = output / f"frame_{len(report['observations']):03}.png"
            path=next_path(path);image.save(path)
            report["observations"].append({"image": str(path), "window": geometry, **result})
            save()
            if not execute:
                break
            action, point = None, None
            state = result["state"]
            if state != "main_menu":
                report.pop("waiting", None)
            if state == "main_menu":
                if closing:
                    report["completed"] = True
                    report["returned_to_main_menu"] = True
                    break
                # Opening a panel is navigation. A delivered click can be ignored
                # by the game; don't wait the full network timeout on a known home.
                if not collect_clicked and not existing_reward:
                    response_waited = time.monotonic() - last_action >= 1.5
                    if entry_attempts >= 3 and response_waited:
                        raise RuntimeError("经营入口点击三次后仍在主页，已停止本项，不继续空等。")
                    if result["entry"] and (entry_attempts == 0 or response_waited):
                        if entry_attempts == 2 and not entry_recovered:
                            # Reuse the ordinary navigation recovery, restricted
                            # to this same verified game window. Observe again
                            # before the final navigation attempt.
                            recovery = window.recover_input(geometry)
                            entry_recovered = True
                            report["actions"].append({"type": "recover_management_entry", **recovery})
                            save()
                            continue
                        action, point = "open_management", result["entry"]["client_point"]
            elif state == "reward":
                if collect_clicked or existing_reward:
                    if sum(a["type"] == "dismiss_reward" for a in report["actions"]) >= 3:
                        raise RuntimeError("Reward page did not close after three confirmation attempts")
                    report["reward_observed"] = True
                    # Verified beside the bottom 'click to return' prompt.
                    action, point = "dismiss_reward", [0.60 * geometry["width"], 0.91 * geometry["height"]]
            elif state == "management_panel":
                panel = result["panel"]
                if panel["state"] == "disabled" and not closing:
                    report["outcome"] = ("collected" if collect_clicked else
                                         "finished_existing_reward" if existing_reward else "skipped_no_rewards")
                    report["disabled_panel_observed"] = True
                if closing or panel["state"] == "disabled":
                    # A click during the collection transition may be ignored.
                    # Re-observe the panel and retry its Cancel, then require home.
                    if close_attempts < 3:
                        action, point = "close_management", panel["cancel"]["client_point"]
                    elif time.monotonic() - last_action >= 1.5:
                        raise RuntimeError("Management panel remained open after three return attempts")
                elif panel["state"] == "enabled" and not collect_clicked and not closing:
                    action, point = "collect_once", panel["collect"]["client_point"]
            repeat_gap = .8  # Network patience must not delay a verified Cancel by 40 seconds.
            if action and time.monotonic() - last_action >= (repeat_gap if action == last_action_kind else .8):
                window.focus_client(geometry)
                fresh_image, fresh_geometry = window.capture()
                fresh = recognizer.inspect(cv2.cvtColor(np.asarray(fresh_image), cv2.COLOR_RGB2BGR))
                valid = fresh_geometry == geometry and fresh['state'] == state
                if valid and state == 'management_panel':
                    button = 'collect' if action == 'collect_once' else 'cancel'
                    valid = (fresh['panel']['state'] == panel['state'] and
                             max(abs(a-b) for a,b in zip(point, fresh['panel'][button]['client_point'])) <= 5)
                    point = fresh['panel'][button]['client_point']
                elif valid and action == 'open_management':
                    valid = bool(fresh.get('entry') and
                                 max(abs(a-b) for a,b in zip(point, fresh['entry']['client_point'])) <= 5)
                    if valid: point = fresh['entry']['client_point']
                if not valid:
                    time.sleep(.2)
                    continue
                event = window.click_client(point, fresh_geometry, press_ms=250)
                report["actions"].append({"type": action, **event})
                last_action = time.monotonic()
                last_action_kind = action
                if action == "open_management":
                    entry_attempts += 1
                    report["entry_attempts"] = entry_attempts
                    report["actions"][-1]["attempt"] = entry_attempts
                    report["waiting"] = {"message": f"等待经营面板打开；已尝试{entry_attempts}/3次，不重复领取奖励。"}
                elif action == "collect_once":
                    collect_clicked = True
                elif action == "close_management":
                    closing = True
                    close_attempts += 1
                    report["return_attempts"] = close_attempts
            save()
            time.sleep(0.25)
        if execute and not report["completed"] and "stopped" not in report:
            if close_attempts >= 3:
                raise RuntimeError("Management panel remained open after three return attempts and the configured wait")
            report["stopped"] = "management_not_verified_within_timeout"
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.image:
        if args.execute:
            parser.error("Cannot execute against a saved image")
        result = ManagementRecognizer().inspect(read_image(args.image.resolve(strict=True)))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
    else:
        from game_window import GameWindow
        from runtime_policy import network_wait_seconds
        report = run_management(GameWindow.find(), args.output, execute=args.execute, timeout=network_wait_seconds())
        print(json.dumps({"completed": report["completed"], "outcome": report["outcome"],
                          "actions": report["actions"], "last_state": report["observations"][-1]["state"] if report["observations"] else None}, ensure_ascii=False))
        if args.execute and not report["completed"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
