"""Worker-local bounded images/OCR and compact reports; durable intents stay on disk."""
import atexit
from collections import OrderedDict
import copy
from io import BytesIO
import hashlib
import json
from pathlib import Path
import time
import uuid
from functools import lru_cache

import cv2
import numpy as np

_session = None
ACCOUNT_BOX = (0, 0, 360, 80)  # Full-client coordinates; next UI row begins at y=87.


def private_mask(image):
    """Exclude the complete avatar/name/power block from full-frame model input."""
    result = image.copy()
    h, w = result.shape[:2]
    if w >= 640 and h >= 360 and abs(w / h - 16 / 9) < .035:
        a, b, c, d = ACCOUNT_BOX
        result[round(b*h/720):round(d*h/720), round(a*w/1280):round(c*w/1280)] = 0
    return result


@lru_cache(maxsize=1)
def _equipment_header():
    from equipment_vision import EquipmentVision
    return EquipmentVision()


def evidence_pixels(frame):
    # Equipment pages replace the account block with the 装备 page title.
    # Its independently matched header is required by restart settlement.
    # Preserve original pixels on those known account-free pages, rather than
    # erasing the very control the durable receipt must prove.
    if _equipment_header().anchor(frame, 'grid_title', .97):
        return frame
    return private_mask(frame)


def activate(output, budget=96*1024*1024):
    global _session
    if _session is not None:
        raise RuntimeError('A frame session is already active')
    _session = Session(output, budget)
    return _session


def active():
    return _session is not None


class Session:
    def __init__(self, output, budget):
        self.output = Path(output).resolve()
        self.budget, self.used, self.sequence = budget, 0, 0
        self.frames, self.ocr, self.reports = OrderedDict(), OrderedDict(), OrderedDict()
        self.identity = uuid.uuid4().hex
        self.stats = dict(captures=0, crops=0, evictions=0, ocr_hits=0,
                          report_writes=0, report_skips=0, durable_frames=0)

    def put(self, path, value):
        key = str(Path(path).resolve())
        size = len(value) if isinstance(value, bytes) else value.width*value.height*len(value.getbands())
        if size > self.budget:
            raise RuntimeError('Frame exceeds the memory image budget')
        if key in self.frames:
            self.used -= self.frames.pop(key)[1]
        while self.frames and self.used + size > self.budget:
            _, (_, amount) = self.frames.popitem(last=False)
            self.used -= amount
            self.stats['evictions'] += 1
        self.frames[key] = (value, size)
        self.used += size


class CaptureImage:
    def __init__(self, image):
        self.image = image

    def __getattr__(self, name):
        return getattr(self.image, name)

    def __array__(self, dtype=None, copy=None):
        array = np.asarray(self.image, dtype=dtype)
        return array.copy() if copy else array

    def save(self, path, *args, **kwargs):
        if active() and isinstance(path, (str, Path)) and Path(path).suffix.lower() == '.png':
            _session.put(path, self.image.copy())
            _session.stats['captures'] += 1
        else:
            self.image.save(path, *args, **kwargs)


def capture_image(image):
    return CaptureImage(image) if active() else image


def next_path(path):
    """Unique per observation even after report history has been trimmed."""
    path = Path(path)
    if not active():
        return path
    _session.sequence += 1
    return path.with_name(f'mem_{_session.identity}_{_session.sequence:08d}_{path.name}')


def is_memory_path(path):
    return Path(path).name.startswith('mem_')


def image_bytes(path):
    key = str(Path(path).resolve())
    if active() and key in _session.frames:
        value, _ = _session.frames[key]
        if isinstance(value, bytes):
            return value
        stream = BytesIO()
        value.save(stream, format='PNG')
        return stream.getvalue()
    if is_memory_path(path) and not Path(path).is_file():
        raise RuntimeError('Observation expired from memory; capture a fresh frame')
    return Path(path).read_bytes()


def write_encoded_png(path, encoded):
    value = encoded.tobytes() if hasattr(encoded, 'tobytes') else bytes(encoded)
    if active():
        _session.put(path, value)
        _session.stats['crops'] += 1
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(value)


def persist(path):
    """Materialize only a commit/receipt frame, before its ledger references it."""
    path = Path(path).resolve()
    if active() and str(path) in _session.frames:
        raw = image_bytes(path)
        frame = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        encoded = cv2.imencode('.png', evidence_pixels(frame))[1].tobytes()
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as handle:
                handle.write(encoded)
                handle.flush()
                import os
                os.fsync(handle.fileno())
            _session.stats['durable_frames'] += 1
        elif path.read_bytes() != encoded:
            raise RuntimeError('Durable frame identity changed')
        # The current process and restart reader must use the same committed pixels.
        _session.put(path, encoded)
    elif is_memory_path(path) and not path.is_file():
        raise RuntimeError('Cannot commit expired frame evidence')
    return path


def persist_references(value):
    if isinstance(value, str) and is_memory_path(value):
        persist(value)
    elif isinstance(value, dict):
        for child in value.values():
            persist_references(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            persist_references(child)


def ocr_get(key):
    if active() and key in _session.ocr:
        _session.ocr.move_to_end(key)
        _session.stats['ocr_hits'] += 1
        return copy.deepcopy(_session.ocr[key])


def ocr_put(key, value):
    if active():
        if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > 512*1024:
            return
        _session.ocr[key] = copy.deepcopy(value)
        _session.ocr.move_to_end(key)
        while len(_session.ocr) > 64:
            _session.ocr.popitem(last=False)


def report_write(path, value, force=False):
    """Keep recent states for existing UI consumers; flush changes and final state."""
    from atomic_json import write_json
    path = Path(path)
    if not active():
        write_json(path, value, _direct=True)
        return
    observations = value.get('observations')
    if isinstance(observations, list) and len(observations) > 16:
        value['observations_dropped'] = value.get('observations_dropped', 0) + len(observations)-16
        del observations[:-16]
    actions = value.get('actions')
    if isinstance(actions, list) and len(actions) > 256:
        value['actions_dropped'] = value.get('actions_dropped', 0) + len(actions)-256
        del actions[:-256]
    # State/event changes are meaningful. Timings, OCR details and frame IDs are not.
    states = [(o.get('observation') or o).get('state') for o in value.get('observations', [])[-2:]]
    signature = json.dumps({k:v for k,v in value.items() if k not in
        ('observations', 'observations_dropped', 'actions_dropped', 'capture_storage', 'waiting')}, sort_keys=True, default=str)
    latest = (observations or [{}])[-1]
    meaningful = {k:latest.get(k) for k in ('decision','input_skipped') if k in latest}
    obs = latest.get('observation', latest)
    meaningful.update({k:obs.get(k) for k in ('state','reason','claimable','free_remaining',
                      'free_available','inventory_count','completed','cost') if k in obs})
    signature = hashlib.sha256((signature+str(states)+json.dumps(meaningful,sort_keys=True)).encode()).hexdigest()
    previous = _session.reports.get(str(path))
    _session.reports[str(path)] = (value, signature)
    _session.reports.move_to_end(str(path))
    while len(_session.reports) > 64:
        _session.reports.popitem(last=False)
    if force or previous is None or previous[1] != signature:
        # Report frames are ephemeral references, never advertised as durable files.
        compact = copy.deepcopy(value)
        def clean(item):
            if isinstance(item, dict):
                for key, child in list(item.items()):
                    if isinstance(child, str) and is_memory_path(child) and not Path(child).is_file():
                        item[key] = None
                        item['frame_storage'] = 'memory_released_after_worker'
                    else:
                        clean(child)
            elif isinstance(item, list):
                for child in item:
                    clean(child)
        clean(compact)
        compact['capture_storage'] = dict(normal_frames='bounded_memory', memory_budget=_session.budget,
                                           **_session.stats)
        write_json(path, compact, _direct=True)
        _session.stats['report_writes'] += 1
    else:
        _session.stats['report_skips'] += 1


def finish(failed=False):
    global _session
    if not active():
        return
    session = _session
    try:
        if failed:
            full = [p for p in session.frames if not isinstance(session.frames[p][0], bytes)]
            for path in full[-2:]:
                persist(path)
        for path, (value, _) in list(session.reports.items()):
            report_write(path, value, force=True)
        from atomic_json import write_json
        write_json(session.output/'capture_metrics.json', dict(session.stats, memory_used=session.used,
                   memory_budget=session.budget, cached_frames=len(session.frames), cached_ocr=len(session.ocr)))
    finally:
        session.frames.clear()
        session.ocr.clear()
        session.reports.clear()
        _session = None


atexit.register(finish)
