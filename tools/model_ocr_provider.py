"""Embedded CPU text recognition; system OCR supplies detection and fallback."""
import atexit
from frame_store import active, image_bytes as frame_bytes, private_mask
import copy
import hashlib
import json
import math
import threading
from pathlib import Path

_lock = threading.RLock()
_engine = None
_engine_key = None
_bundle_stamp = None
_bundle_digest = None
PROVIDER_VERSION = 'embedded_aligned_v2_1'


def release_model():
    global _engine, _engine_key
    with _lock:
        _engine, _engine_key = None, None


atexit.register(release_model)


def _settings(root):
    path = root / 'config/model_recognition.json'
    if not path.is_file():
        return None
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    if config.get('enabled') is not True:
        return None
    threads = config.get('threads', 2)
    if type(threads) is not int or not 1 <= threads <= 4:
        raise ValueError('Model threads must be an integer from 1 to 4')
    if config.get('runtime', 'onnx_cpu') != 'onnx_cpu':
        raise ValueError('Only embedded CPU inference is supported')
    return {'threads': threads, 'minimum_score': .85}


def bundle_identity(bundle):
    global _bundle_stamp, _bundle_digest
    paths = [bundle / name for name in ('ocr.onnx', 'visual.onnx', 'dictionary.txt')]
    stamp = tuple((str(p.resolve()), p.stat().st_size, p.stat().st_mtime_ns) for p in paths)
    if stamp != _bundle_stamp:
        digest = hashlib.sha256()
        for path in paths:
            digest.update(path.name.encode('utf-8'))
            with path.open('rb') as handle:
                digest.update(hashlib.file_digest(handle, 'sha256').digest())
        _bundle_stamp, _bundle_digest = stamp, digest.hexdigest()
    return _bundle_digest


def _accepted(prediction, minimum):
    score = prediction.get('score')
    return (isinstance(prediction.get('text'), str) and bool(prediction['text'].strip())
            and isinstance(score, (int, float)) and math.isfinite(score) and score >= minimum)


def _cache_valid(payload, width, height, language, identity):
    return (isinstance(payload, dict) and payload.get('width') == width
            and payload.get('height') == height and payload.get('language') == language
            and payload.get('model_identity') == identity
            and payload.get('provider_version') == PROVIDER_VERSION
            and isinstance(payload.get('lines'), list)
            and all(isinstance(line, dict) and isinstance(line.get('text'), str)
                    and isinstance(line.get('words'), list) for line in payload['lines']))


def model_ocr(path, output, language, system_ocr):
    from project_paths import ROOT
    path, output = Path(path), Path(output)
    with _lock:
        try:
            config = _settings(ROOT)
            if config is None:
                release_model()
            else:
                bundle = ROOT / 'models/gamevision_bd2'
                identity = bundle_identity(bundle)
        except (OSError, ValueError, TypeError, AttributeError) as error:
            release_model()
            config = None
        if config is None:
            return system_ocr(path, output, language)
        system_failed = False
        def detect():
            nonlocal system_failed
            try:
                return system_ocr(path, output, language)
            except Exception:
                system_failed = True
                raise
        try:
            import cv2
            import numpy as np
            image_bytes = frame_bytes(path)
            image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
            if image is None or not image.size:
                raise ValueError('Unreadable model input image')
            if active():
                image = private_mask(image)
                image_bytes = cv2.imencode('.png', image)[1].tobytes()
            height, width = image.shape[:2]
            key = hashlib.sha256(image_bytes + identity.encode() + PROVIDER_VERSION.encode()
                                 + language.encode() + str(config).encode()).hexdigest()
            cache = output / 'model_aligned_v2' / (key + '.json')
            try:
                if not active() and cache.is_file() and cache.stat().st_size <= 5_000_000:
                    result = json.loads(cache.read_text(encoding='utf-8'))
                    if _cache_valid(result, width, height, language, identity):
                        result['model_cache_hit'] = True
                        return result
            except (OSError, ValueError, TypeError):
                pass
            global _engine, _engine_key
            engine_key = (identity, config['threads'])
            if _engine is None or _engine_key != engine_key:
                from gamevision_runtime import GameVision
                _engine = GameVision(bundle, threads=config['threads'])
                _engine_key = engine_key
            if height <= 256 and width <= 1600:
                prediction = _engine.read_text(image)
                if not _accepted(prediction, config['minimum_score']):
                    return detect()
                result = {'width':width, 'height':height, 'language':language,
                    'backend':'gamevision_bd2_aligned_v2', 'model_score':prediction['score'],
                    'score_type':'uncalibrated_character_score', 'geometry_kind':'crop_extent',
                    'lines':[{'text':prediction['text'], 'words':[{'text':prediction['text'],
                        'x':0, 'y':0, 'width':width, 'height':height}]}]}
            else:
                # Detector failures propagate; the model cannot invent text locations.
                result = copy.deepcopy(detect())
                if abs(width / height - 16 / 9) > .035:
                    return result
                normalized = _engine.align(image)
                corrections, failures = [], 0
                for line in result['lines']:
                    words = line.get('words', [])
                    if not words:
                        continue
                    x0, y0 = min(w['x'] for w in words), min(w['y'] for w in words)
                    x1 = max(w['x'] + w['width'] for w in words)
                    y1 = max(w['y'] + w['height'] for w in words)
                    a, b = max(0, round(x0*1280/width)-2), max(0, round(y0*720/height)-2)
                    c, d = min(1280, round(x1*1280/width)+2), min(720, round(y1*720/height)+2)
                    if not (6 <= c-a <= 750 and 8 <= d-b <= 70):
                        continue
                    try:
                        prediction = _engine.read_text(normalized[b:d, a:c])
                    except Exception:
                        failures += 1
                        continue
                    if _accepted(prediction, config['minimum_score']):
                        old = line['text']
                        line['system_text'] = old
                        line['text'] = prediction['text']
                        line['model_score'] = prediction['score']
                        # Preserve every original detector word and its coordinates.
                        # OcrDocument uses corrected line.text and the same union box.
                        if old != line['text']:
                            corrections.append({'before':old, 'after':line['text'],
                                'box_1280':[a,b,c,d], 'score':prediction['score']})
                result.update(backend='system_detection_with_gamevision_bd2_recognition',
                              model_corrections=corrections, model_line_failures=failures)
                if failures:
                    return result
            result.update(model_identity=identity, provider_version=PROVIDER_VERSION,
                          model_cache_hit=False, model_threads=config['threads'])
            try:
                from atomic_json import write_json
                if not active():
                    write_json(cache, result)
            except OSError:
                pass
            return result
        except Exception as error:
            # No helper/server process is started here, including on failure.
            # An existing detector/stop error must retain its normal propagation.
            from execution_control import ExecutionControlError
            if system_failed or isinstance(error, ExecutionControlError):
                raise
            release_model()
            result = copy.deepcopy(system_ocr(path, output, language))
            result['model_fallback_reason'] = type(error).__name__ + ': ' + str(error)
            return result
