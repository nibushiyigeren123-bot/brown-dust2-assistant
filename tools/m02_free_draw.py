"""Free costume/equipment batches, with UI checks before every click.

Default is read-only. --execute enables a bounded run; F8 stops live input.
"""
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


class FreeDrawRecognizer:
    def __init__(self):
        assets = ROOT / 'assets' / 'free_draw'
        config = json.loads((assets / 'config.json').read_text(encoding='utf-8'))
        self.templates = {name: read_image(assets / spec['file']) for name, spec in config.items()}
        self.navigation = PageDiagnosis()

    def match(self, frame, name, roi, threshold=0.80, foreground=False, brightness=0):
        x1, y1, x2, y2 = roi
        region = frame[y1:y2, x1:x2]
        template = cv2.cvtColor(self.templates[name], cv2.COLOR_BGR2GRAY)
        best = None
        for factor in (0.96, 1.0, 1.04):
            patch = cv2.resize(template, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
            ph, pw = patch.shape
            if ph > region.shape[0] or pw > region.shape[1]:
                continue
            if foreground:
                result = masked_shape_match(region, foreground_mask(patch))
                score, position = result['score'], result['position']
            else:
                values = cv2.matchTemplate(region, patch, cv2.TM_CCOEFF_NORMED)
                _, score, _, position = cv2.minMaxLoc(values)
            px, py = position
            ratio = float(region[py:py+ph, px:px+pw].mean() / max(patch.mean(), 1))
            if best is None or score > best['score']:
                best = {'score': float(score), 'brightness_ratio': ratio,
                        'point_1280': [x1 + px + pw / 2, y1 + py + ph / 2],
                        'box_1280': [x1 + px, y1 + py, pw, ph]}
        if best and best['score'] >= threshold and best['brightness_ratio'] >= brightness:
            return best
        return None

    def inspect(self, image):
        h, w = image.shape[:2]
        if abs(w / h - 16 / 9) > 0.035:
            return {'state': 'unknown', 'reason': 'unsupported_aspect_ratio'}
        normalized = cv2.resize(image, (1280, 720), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY)

        def point(match):
            return [match['point_1280'][0] * w / 1280, match['point_1280'][1] * h / 720]

        dialog = self.match(gray, 'confirm_title', (570, 274, 710, 325))
        if dialog:
            free = self.match(gray, 'free_word', (592, 360, 669, 408), threshold=0.85)
            confirm = self.match(gray, 'confirm_button', (665, 416, 728, 460), threshold=0.80)
            cancel = self.match(gray, 'cancel_button', (550, 416, 615, 460), threshold=0.84)
            # The recording cursor covers confirm in some frames; a matched cancel
            # label establishes the paired button geometry without using its cursor.
            if not confirm and cancel:
                cx, cy = cancel['point_1280']
                confirm = {'point_1280': [cx + 114, cy]}
            yellow = False
            if free:
                x, y, bw, bh = [round(v) for v in free['box_1280']]
                hsv = cv2.cvtColor(normalized[y:y+bh, x:x+bw], cv2.COLOR_BGR2HSV)
                yellow = float(np.mean((hsv[:, :, 0] >= 15) & (hsv[:, :, 0] <= 40)
                                       & (hsv[:, :, 1] > 80) & (hsv[:, :, 2] > 90))) > 0.12
            return {'state': 'free_confirmation' if free and confirm and yellow else 'unverified_confirmation',
                    'dialog': dialog, 'free_word': free, 'yellow_free_word': yellow,
                    'confirm_point': point(confirm) if confirm else None}

        back = self.match(gray, 'back', (90, 7, 139, 63), threshold=0.74, foreground=True)
        badge = (self.match(gray, 'result_badge', (84, 605, 159, 676), threshold=0.79)
                 or self.match(gray, 'equipment_result_badge', (84, 605, 159, 676), threshold=0.79))
        # Results only become actionable after the back control and points badge appear.
        pale = float(np.mean((gray[70:600, 300:1150] > 125)
                             & (cv2.cvtColor(normalized[70:600, 300:1150], cv2.COLOR_BGR2HSV)[:, :, 1] < 55)))
        if back and badge and pale > 0.70:
            return {'state': 'draw_result', 'back_point': point(back), 'pale_fraction': pale}

        titles = {kind: self.match(gray, kind + '_title', (177, 10, 244, 62), brightness=0.80)
                  for kind in ('costume', 'equipment')}
        candidates = [(m['score'], kind, m) for kind, m in titles.items() if m]
        paid = self.match(gray, 'paid_label', (750, 615, 878, 669), threshold=0.77, brightness=0.77)
        clothes = self.match(gray, 'costume_tab', (88, 207, 147, 257), threshold=0.76)
        equipment = self.match(gray, 'equipment_tab', (88, 280, 147, 330), threshold=0.76)
        if candidates and paid and clothes and equipment and back:
            _, kind, title = max(candidates)
            batch = self.match(gray, 'batch_free', (170, 620, 310, 674), threshold=0.82, brightness=0.78)
            return {'state': 'gacha_page', 'category': kind, 'free_available': batch is not None,
                    'free_point': point(batch) if batch else None,
                    'tab_points': {'costume': point(clothes), 'equipment': point(equipment)},
                    'back_point': point(back), 'title': title, 'batch': batch}

        # Skip is a UI glyph (double triangle + bar), next to the animation loop
        # control. Foreground masks exclude the changing costume/scene background.
        skips = [self.match(gray, 'skip_' + variant, (1158, 18, 1206, 62),
                            threshold=0.84, foreground=True) for variant in ('light', 'dark')]
        loops = [self.match(gray, 'animation_loop_' + variant, (1080, 15, 1135, 65),
                            threshold=0.62, foreground=True) for variant in ('light', 'dark')]
        skips, loops = [m for m in skips if m], [m for m in loops if m]
        if skips and loops:
            skip = max(skips, key=lambda m: m['score'])
            return {'state': 'draw_animation', 'skip_point': point(skip),
                    'skip_control': skip, 'animation_loop': max(loops, key=lambda m: m['score'])}

        menu = self.navigation.opening.menu.inspect(image)
        if menu['state'] == 'main_menu':
            entry = self.match(gray, 'home_entry', (70, 660, 146, 704), threshold=0.74, foreground=True)
            if entry:
                p = point(entry)
                p[1] -= 22 * h / 720
                return {'state': 'main_menu', 'entry_point': p}
            return {'state': 'main_menu', 'entry_point': None}
        return {'state': 'unknown', 'reason': 'no_actionable_free_draw_ui'}


class FreeDrawController:
    """Observation-driven state; no input in this class, reusable for video replay."""
    def __init__(self):
        self.categories = {k: {'phase': 'unseen', 'outcome': None, 'skip_issued': False}
                           for k in ('costume', 'equipment')}
        self.active = None
        self.opened = False
        self.closing = False
        self.completed = False
        self.absent_kind = None
        self.absent_since = None
        self.absent_count = 0
        self.switch_target = None
        self.return_attempts = 0
        self.result_close_attempts = {k: 0 for k in self.categories}

    def step(self, observation, now):
        state = observation['state']
        if state != 'gacha_page' or observation.get('free_available'):
            self.absent_kind, self.absent_since, self.absent_count = None, None, 0
        if state == 'main_menu':
            if self.closing:
                self.completed = True
            elif not self.opened and observation.get('entry_point'):
                self.opened = True
                return {'type': 'open_gacha', 'point': observation['entry_point']}
        elif state == 'free_confirmation':
            if self.active and self.categories[self.active]['phase'] == 'opening_free':
                self.categories[self.active]['phase'] = 'confirmed'
                return {'type': 'confirm_free_batch', 'category': self.active,
                        'point': observation['confirm_point']}
        elif state == 'unverified_confirmation':
            if self.active and self.categories[self.active]['phase'] == 'opening_free':
                raise RuntimeError('Confirmation is not verified as free; stopped without confirming')
        elif state == 'draw_animation':
            if self.active:
                item = self.categories[self.active]
                if item['phase'] == 'confirmed' and not item['skip_issued']:
                    item['skip_issued'] = True
                    return {'type': 'skip_free_animation', 'category': self.active,
                            'point': observation['skip_point']}
        elif state == 'draw_result':
            if (self.active and self.categories[self.active]['phase'] in ('confirmed', 'dismissing_result')
                    and self.result_close_attempts[self.active] < 3):
                self.categories[self.active]['phase'] = 'dismissing_result'
                self.result_close_attempts[self.active] += 1
                return {'type': 'close_free_result', 'category': self.active, 'point': observation['back_point']}
        elif state == 'gacha_page':
            if self.closing:
                if self.return_attempts < 3:
                    self.return_attempts += 1
                    return {'type': 'return_from_gacha', 'point': observation['back_point']}
                return None
            kind = observation['category']
            if self.switch_target and kind != self.switch_target:
                return None
            self.switch_target = None
            self.active = kind
            item = self.categories[kind]
            if item['phase'] in ('opening_free', 'confirmed'):
                return None  # Disappearance during a submitted draw does not establish completion.
            if observation['free_available']:
                if item['phase'] == 'dismissing_result':
                    raise RuntimeError('Free entry persists after results; do not submit another batch')
                if item['phase'] == 'unseen':
                    item['phase'] = 'opening_free'
                    return {'type': 'open_free_batch', 'category': kind, 'point': observation['free_point']}
            elif item['phase'] != 'done':
                if self.absent_kind != kind:
                    self.absent_kind, self.absent_since, self.absent_count = kind, now, 0
                self.absent_count += 1
                if self.absent_count < 3 or now - self.absent_since < 1.2:
                    return None
                item['outcome'] = 'collected' if item['phase'] == 'dismissing_result' else 'already_finished_today'
                item['phase'] = 'done'
            if item['phase'] == 'done' and not self.closing:
                remaining = [k for k, v in self.categories.items() if v['phase'] != 'done']
                if remaining:
                    self.switch_target = remaining[0]
                    return {'type': 'switch_category', 'category': remaining[0],
                            'point': observation['tab_points'][remaining[0]]}
                self.closing = True
                self.return_attempts += 1
                return {'type': 'return_from_gacha', 'point': observation['back_point']}
        return None


def run_free_draw(window, output, execute=False, timeout=90):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    recognizer = FreeDrawRecognizer()
    controller = FreeDrawController()
    report = {'execute': execute, 'completed': False, 'actions': [], 'observations': []}

    def save():
        report['categories'] = controller.categories
        report['completed'] = controller.completed
        report_write(output / 'run.json', report)

    try:
        im, _ = window.capture()
        initial = recognizer.inspect(cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR))
        if execute and initial['state'] not in ('main_menu', 'gacha_page'):
            navigation = ensure_main_menu(window, output / 'navigation', execute=True, timeout=timeout)
            if not navigation['completed']:
                raise RuntimeError('Could not establish main menu before free draw')
        deadline = time.monotonic() + timeout
        next_input = 0.0
        while time.monotonic() < deadline:
            import win32api
            if win32api.GetAsyncKeyState(0x77) & 0x8000:
                report['stopped'] = 'F8'
                break
            im, geometry = window.capture()
            result = recognizer.inspect(cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR))
            path = output / f"frame_{len(report['observations']):03}.png"
            path=next_path(path);im.save(path)
            report['observations'].append({'image': str(path), 'window': geometry, **result})
            save()
            if not execute:
                break
            now = time.monotonic()
            if now >= next_input:
                action = controller.step(result, now)
                if action:
                    event = window.click_client(action['point'], geometry)
                    report['actions'].append({**action, **event})
                    next_input = time.monotonic() + 0.9
                if controller.completed:
                    break
            save()
            time.sleep(0.25)
        if execute and not controller.completed and 'stopped' not in report:
            report['stopped'] = 'free_draw_not_verified_within_timeout'
    except Exception as error:
        report['error'] = str(error)
        raise
    finally:
        save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path)
    parser.add_argument('--execute', action='store_true')
    from runtime_policy import network_wait_seconds
    parser.add_argument('--timeout', type=float, default=max(90, network_wait_seconds() * 2))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.image:
        if args.execute:
            parser.error('A saved image cannot receive input')
        result = FreeDrawRecognizer().inspect(read_image(args.image.resolve(strict=True)))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
    else:
        from game_window import GameWindow
        report = run_free_draw(GameWindow.find(), args.output, execute=args.execute, timeout=args.timeout)
        print(json.dumps({k: report.get(k) for k in ('completed', 'categories', 'actions', 'stopped')}, ensure_ascii=False))
        if args.execute and not report['completed']:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
