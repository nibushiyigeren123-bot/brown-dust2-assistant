"""Offline embedded-model smoke checks; no window discovery or game input."""
import hashlib
import json
import os
import time
from pathlib import Path


def check_embedded_model(output, image, expected=None, hold=0):
    if image is None or not 0 <= hold <= 60:
        raise ValueError('An offline model image and hold from 0 to 60 seconds are required')
    from daily_vision import windows_ocr
    import model_ocr_provider
    from assistant_lifetime import membership
    from m00_recognize import ROOT, read_image
    import numpy as np
    output, image = Path(output), Path(image).resolve(strict=True)
    output.mkdir(parents=True, exist_ok=True)
    prediction = windows_ocr(image, output / 'ocr')
    assert prediction['backend'] == 'gamevision_bd2_aligned_v2', prediction
    text = ''.join(line['text'] for line in prediction['lines'])
    if expected is not None:
        assert text == expected, (text, expected)
    engine = model_ocr_provider._engine
    # A valid disk cache may bypass construction; explicitly load this process's
    # sessions for the lifetime check, without starting another executable.
    if engine is None:
        from gamevision_runtime import GameVision
        engine = GameVision(ROOT / 'models/gamevision_bd2')
        engine.read_text(read_image(image))
    vector = engine.image_vector(read_image(ROOT / 'assets/equipment/quality_R_plain.png'))
    assert vector.ndim == 1 and np.isfinite(vector).all() and abs(float(np.linalg.norm(vector))-1) < .001
    providers = {name:engine.session(name).get_providers() for name in ('ocr','visual')}
    assert all(value == ['CPUExecutionProvider'] for value in providers.values()), providers
    report = {'pid':os.getpid(), 'backend':prediction['backend'], 'text':text,
              'expected':expected, 'image_sha256':hashlib.sha256(image.read_bytes()).hexdigest(),
              'model_identity':prediction['model_identity'], 'providers':providers,
              'model_in_same_process':True, 'model_server_started':False,
              'visual_vector_size':len(vector), 'lifetime':membership(), 'game_input':False}
    (output / 'model_check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    time.sleep(hold)
    model_ocr_provider.release_model()
    return report
