"""Equipment execution and local-date receipts. Read-only unless --execute."""
from frame_store import next_path, report_write, persist_references
import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from local_cache import cache_path, load_local_config

from daily_tasks import guard_foreign_pending, load_config as load_daily
from equipment_rules import EquipmentProgress
from equipment_vision import EquipmentVision
from game_window import GameWindow
from m00_recognize import ROOT
from pending_daily import exclusive_ledger


def save_json(path, data):
    from atomic_json import write_json
    write_json(path, data, fsync=True, newline='\n')


class EquipmentLedger:
    def __init__(self, data_root):
        daily = load_daily(data_root / 'config/daily_tasks.json')
        day = daily['cache_date']
        self.path = cache_path(data_root, daily, 'equipment')
        self.data = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {
            'schema_version': 1, 'cache_date': day, 'pending': None,
            'equipment': EquipmentProgress().state, 'events': [], 'selected_quality': None,
        }
        if self.data['cache_date'] != day:
            raise RuntimeError('Equipment local-cache date mismatch')
        self.progress = EquipmentProgress(self.data['equipment'])

    def save(self):
        save_json(self.path, self.data)

    def submit_refine(self, observation, image):
        if self.data['pending'] or self.progress.state['refine_submitted']:
            raise RuntimeError('Refinement already submitted; observe its receipt without retrying')
        # [门禁] 与既有装备规则一致：只接受1..23完整纯数字，不限定为旧样本20分。
        if type(observation.get('score')) is not int or not 1<=observation['score']<24:
            raise RuntimeError('Refinement score missing or already at 24')
        before, cost = observation['before'], observation['cost']
        if any(type(v) is not int for v in (*before.values(), *cost.values())):
            raise RuntimeError('Exact resource balances and costs are required')
        if not cost or any(v <= 0 or before[k] < v for k, v in cost.items()):
            raise RuntimeError('Insufficient or uncertain refinement resources')
        self.progress.submit_refine()
        self.data['pending'] = {'module': 'equipment', 'stage': 'refine', 'before': before,
                                'cost': cost, 'score_before': observation['score'],
                                'evidence': str(image), 'input_receipt': None}
        self.save()

    def settle_refine(self, observation, image):
        pending = self.data['pending']
        if not pending or pending['stage'] != 'refine':
            return False
        after = observation.get('before', {})
        if any(type(after.get(k)) is not int or pending['before'][k] - after[k] != v
               for k, v in pending['cost'].items()):
            return False
        self.progress.confirm_refine(pending['before'], after, pending['cost'])
        self.data['events'].append({'submission': pending, 'after': after, 'evidence': str(image)})
        self.data['pending'] = None
        self.save()
        return True

    def record_filter(self, qualities, image, *, saved, worn_hidden):
        if self.data['pending'] or qualities not in (['R'], ['N']) or not saved or not worn_hidden:
            raise RuntimeError('A saved single-quality filter excluding equipped items is required')
        self.data['selected_quality'] = qualities[0]
        self.data['filter_evidence'] = {'qualities': qualities, 'image': str(image),
                                        'saved': True, 'worn_hidden': True}
        self.save()

    def record_detail_filter(self,qualities,before,grid,geometry):
        """[新筛选契约] 应用前单选全帧+确认后普通网格，不要求改动穿戴显示设置。"""
        from equipment_receipts import image_receipt,verify_detail_filter
        proof={'basis':'detail_filter_applied','qualities':qualities,
               'before':image_receipt(before),'grid':image_receipt(grid),'geometry':geometry}
        if self.data['pending'] or not verify_detail_filter(EquipmentVision(),proof):
            raise RuntimeError('详情路线筛选应用证据不足，未建立分解资格。')
        self.data['selected_quality']=qualities[0]
        self.data['detail_filter_evidence']=proof
        self.save()

    def submit_decompose(self, observation, image):
        if self.data['pending']:
            raise RuntimeError('Reconcile existing submission without retrying')
        qualities = observation['selected_qualities']
        if qualities != [self.data.get('selected_quality')]:
            raise RuntimeError('Saved filter differs from the inspected item')
        inventory = observation.get('inventory_count')
        if inventory is not None and (type(inventory) is not int or not 0 <= inventory <= 2000):
            raise RuntimeError('Invalid auxiliary inventory count')
        # [新basis] 即使库存数字可读，也必须验证完整详情单件图像链，不能仅信任传入布尔值。
        if (inventory is None or observation.get('visual_receipt', {}).get('basis')
                in ('filtered_selectable_item','detail_single_item')):
            from equipment_receipts import verify_selection
            try:
                verified = verify_selection(EquipmentVision(), observation, image)
            except (OSError, KeyError, TypeError, ValueError):
                verified = False
            if not verified:
                raise RuntimeError('Inventory unavailable and image-backed single R selection is incomplete')
        self.progress.submit_decompose(observation['card'], qualities, observation['selected_count'],
                                       r_unavailable=observation.get('r_unavailable', False))
        self.data['pending'] = {'module': 'equipment', 'stage': 'decompose',
                                'observation': observation, 'evidence': str(image), 'actions': []}
        self.save()

    def settle_decompose(self, inventory_count, result_evidence, image):
        pending = self.data['pending']
        if not pending or pending['stage'] != 'decompose':
            return False
        before = pending['observation'].get('inventory_count')
        if not result_evidence:
            return False
        # [新basis结算] 确认/输入/结果/返回原图链一律重核；库存减少不能替代证据。
        if pending['observation'].get('visual_receipt',{}).get('basis')=='detail_single_item':
            from equipment_receipts import verify_visual_settlement
            if not verify_visual_settlement(pending,result_evidence,image):return False
        known_counts = type(before) is int and type(inventory_count) is int
        if known_counts:
            if before - inventory_count != 1:
                return False
        else:
            from equipment_receipts import verify_visual_settlement
            if not verify_visual_settlement(pending, result_evidence, image):
                return False
        self.progress.confirm_decompose(1)
        self.data['events'].append({'submission': pending, 'inventory_after': inventory_count,
                                    'result_evidence': str(result_evidence), 'evidence': str(image),
                                    'settlement_basis': 'inventory_delta' if known_counts else 'single_R_visual_receipts'})
        self.data['pending'] = None
        self.save()
        return True


def probe_refine_once(output, data_root):
    """One explicitly authorized test from an already inspected non-24 UR page."""
    output.mkdir(parents=True, exist_ok=False)
    report = {'scope': 'single refinement only', 'execute': True, 'completed': False,
              'actions': [], 'observations': []}
    vision = EquipmentVision()
    window = GameWindow.find()
    with exclusive_ledger(data_root / 'runtime/game_control'):
        ledger = EquipmentLedger(data_root)
        guard_foreign_pending(data_root / 'runtime', ledger.path, module='equipment')
        report['ledger'] = str(ledger.path)
        try:
            for _ in range(2):
                image, geometry = window.capture()
                path = output / f"before_{len(report['observations']):03}.png"
                path=next_path(path);image.save(path)
                obs = vision.inspect_file(path, output)
                assert obs['state'] == 'equipment_refine' and obs['score'] != 24
                report['observations'].append({'image': str(path), 'observation': obs})
            assert report['observations'][0]['observation'] == obs, 'UI changed before submission'
            ledger.submit_refine(obs, path)
            x, y = obs['points']['submit']
            event = window.click_client([x * geometry['width'] / 1280, y * geometry['height'] / 720], geometry)
            ledger.data['pending']['input_receipt'] = event
            ledger.save()
            report['actions'].append({'type': 'refine_once', 'score_before': obs['score'], **event})
            save_json(output / 'run.json', report)
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                time.sleep(1)
                image, geometry = window.capture()
                path = output / f"after_{len(report['observations']):03}.png"
                path=next_path(path);image.save(path)
                obs = vision.inspect_file(path, output)
                report['observations'].append({'image': str(path), 'observation': obs})
                if ledger.settle_refine(obs, path):
                    report['completed'] = True
                    break
            if not report['completed']:
                report['stopped'] = 'Refinement submitted; exact consumption not yet verified'
        finally:
            report['progress'] = ledger.data
            save_json(output / 'run.json', report)
    print(json.dumps({k: v for k, v in report.items() if k not in ('observations', 'progress')}, ensure_ascii=False))
    if not report['completed']:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe-refine-once', action='store_true')
    parser.add_argument('--image', type=Path)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--data-root', type=Path, default=ROOT)
    args = parser.parse_args()
    output, data_root = args.output.resolve(), args.data_root.resolve(strict=True)
    if args.probe_refine_once:
        if args.image:
            parser.error('The explicitly authorized refinement probe cannot use a saved image')
        return probe_refine_once(output, data_root)
    if args.image and args.execute:
        parser.error('Saved equipment screenshots are read-only')
    output.mkdir(parents=True, exist_ok=True)
    report = {'execute': args.execute, 'completed': False, 'actions': [], 'observations': [],
              'scope': 'equipment recognition and current-cycle progress; general execution not released'}
    with exclusive_ledger(data_root / 'runtime/game_control'):
        ledger = EquipmentLedger(data_root)
        if args.image:
            path = args.image.resolve(strict=True)
        else:
            image, geometry = GameWindow.find().capture()
            path = output / 'current.png'
            path=next_path(path);image.save(path)
            report['geometry'] = geometry
        observation = EquipmentVision().inspect_file(path, output / 'recognition')
        report.update(cache_date=ledger.data['cache_date'],
                      ledger=str(ledger.path), progress=ledger.progress.state.copy(), pending=ledger.data['pending'])
        report['observations'].append({'image': str(path), 'observation': observation})
        save_json(output / 'inspection.json', {'observation': observation, 'progress': report['progress'],
                                              'cache_date': report['cache_date']})
        if args.execute:
            try:
                guard_foreign_pending(data_root / 'runtime', ledger.path, module='equipment')
                if ledger.data['pending']:
                    report['stopped'] = {'status': 'needs_review',
                                         'reason': 'Equipment has a pending action; reconcile it without retrying'}
                elif all(ledger.progress.state.get(k) for k in
                         ('refine_completed', 'decompose_completed', 'filter_initialized')):
                    report['completed'] = observation['state'] == 'main_menu'
                    if not report['completed']:
                        report['stopped'] = {'status': 'needs_review',
                                             'reason': 'Equipment cycle is complete; return to menu without repeating it'}
                else:
                    from equipment_workflow import EquipmentRun
                    EquipmentRun(GameWindow.find(),ledger,report,output,
                                 lambda:save_json(output/'run.json',report)).execute()
            except RuntimeError as error:
                report['stopped'] = {'status': 'needs_review', 'reason': str(error)}
            finally:
                report['progress']=ledger.progress.state.copy()
                report['pending']=ledger.data['pending']
        save_json(output / 'run.json', report)
    print(json.dumps({k: v for k, v in report.items() if k != 'observations'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
