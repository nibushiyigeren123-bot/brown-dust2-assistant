"""Queue-owned navigation goal using the shared masked reference set and CPU model."""
import json
from pathlib import Path
import cv2
import numpy as np
from project_paths import ROOT
from frame_store import private_mask
from functools import lru_cache


@lru_cache(maxsize=1)
def context_for_goal():
    return RecognitionContext()


class RecognitionContext:
    def __init__(self, goal='main_menu', queue_task=None):
        if goal != 'main_menu':
            raise ValueError('Unsupported navigation goal')
        import os
        self.goal = goal
        self.queue_task = queue_task or os.environ.get('BD2_QUEUE_TASK', 'navigation')
        self.engine = None
        self.references = None
        self.manifest = None
        self.images = {}

    def identify(self, image):
        h,w = image.shape[:2]
        if w < 640 or h < 360 or abs(w/h-16/9) > .025:
            return dict(goal=self.goal, queue_task=self.queue_task, available=False,
                        reason='unsupported_client_geometry')
        if self.references is None:
            folder = ROOT/'assets/shared_references'
            self.manifest = json.loads((folder/'index.json').read_text(encoding='utf-8'))
            from model_ocr_provider import _settings
            settings = _settings(ROOT)
            if settings is None:
                return dict(goal=self.goal, queue_task=self.queue_task, available=False,
                            reason='embedded_model_disabled')
            from gamevision_runtime import GameVision
            self.engine = GameVision(ROOT/'models/gamevision_bd2', threads=settings['threads'])
            references, images = {}, {}
            for key, spec in self.manifest['templates'].items():
                # Global embeddings propose pages; independent UI evidence authorizes exits.
                import hashlib
                raw = (folder/spec['file']).read_bytes()
                if hashlib.sha256(raw).hexdigest() != spec['sha256']:
                    raise ValueError('Shared reference changed')
                frame = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
                if frame is None or frame.shape[:2] != (720,1280):
                    raise ValueError('Invalid full-client reference')
                frame = private_mask(frame)
                if spec.get('navigation'):
                    images[key] = frame
                references[key] = self.engine.image_vector(frame)
            self.references, self.images = references, images
        result = self.engine.match(private_mask(image), self.references, minimum=.94)
        # Supervised page families are additional evidence. Existing independent
        # controls retain all action authority; a hint cannot make a click.
        from command_vision import scene_hint
        result['trained_scene'] = scene_hint(image)
        result.update(goal=self.goal, queue_task=self.queue_task, available=True,
                      account_region_excluded=True)
        if result['match']:
            result['state'] = self.manifest['templates'][result['match']]['state']
        return result

    def correction(self, image, result):
        key = result.get('match')
        spec = self.manifest['templates'].get(key, {}) if self.manifest else {}
        exit_spec = spec.get('navigation')
        if not exit_spec:
            return None
        if (spec.get('state') not in ('pass_tasks','pass_levels')
                or exit_spec.get('action') != 'close_panel' or exit_spec.get('point') != [50,360]
                or exit_spec.get('anchors') != [[212,181,352,316],[1037,179,1086,539]]):
            return None
        frame = cv2.resize(private_mask(image), (1280,720), interpolation=cv2.INTER_AREA)
        reference = self.images[key]
        # Two distinct static UI patches must agree, independent of wallpaper similarity.
        scores = []
        for a,b,c,d in exit_spec['anchors']:
            patch, template = frame[b:d,a:c], reference[b:d,a:c]
            score = float(cv2.matchTemplate(patch, template, cv2.TM_CCOEFF_NORMED)[0,0])
            if not np.isfinite(score) or score < .96:
                return None
            gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
            ink = gray > max(80, np.percentile(gray,70))
            if not ink.any() or float(cv2.cvtColor(patch,cv2.COLOR_BGR2GRAY)[ink].mean()) < .85*float(gray[ink].mean()):
                return None
            scores.append(round(score,4))
        h,w = image.shape[:2]
        x,y = exit_spec['point']
        return dict(state=spec['state'], next_action=exit_spec['action'],
                    return_button=dict(client_point=[x*w/1280,y*h/720], scores=scores),
                    recognition=result)

    def diagnose(self, image, existing):
        import os
        self.queue_task = os.environ.get('BD2_QUEUE_TASK', self.queue_task)
        # This model is a matcher, not an instruction-following coordinate planner.
        # Existing current-frame UI controls remain the independent action evidence.
        try:
            result = self.identify(image)
        except Exception as error:
            from execution_control import ExecutionControlError
            if isinstance(error, ExecutionControlError):
                raise
            return dict(existing, recognition=dict(goal=self.goal, queue_task=self.queue_task,
                        available=False, reason=type(error).__name__))
        if existing['state'] == self.goal:
            return dict(existing, goal=self.goal, queue_task=self.queue_task,
                        recognition=result)
        if existing['next_action'] == 'wait' and result.get('available'):
            correction = self.correction(image, result)
            if correction:
                return correction
        return dict(existing, goal=self.goal, queue_task=self.queue_task, recognition=result)
