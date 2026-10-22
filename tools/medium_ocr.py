"""PP-OCRv6 Medium CPU OCR; lazy sessions, no server and no screen input."""
import json
from pathlib import Path
import time
import cv2
import numpy as np
import onnxruntime as ort
import pyclipper


class MediumOCR:
    def __init__(self, bundle, threads=2):
        self.bundle = Path(bundle)
        self.threads = threads
        self._ocr = self._detector = None
        self.characters = ['blank'] + json.loads(
            (self.bundle / 'medium_dictionary.json').read_text(encoding='utf-8')) + [' ']

    def session(self, name):
        attribute = '_ocr' if name == 'ocr' else '_detector'
        session = getattr(self, attribute)
        if session is None:
            options = ort.SessionOptions()
            options.intra_op_num_threads = self.threads
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.log_severity_level = 3
            session = ort.InferenceSession(str(self.bundle / ('medium_rec.onnx' if name == 'ocr' else 'medium_det.onnx')),
                sess_options=options, providers=['CPUExecutionProvider'])
            setattr(self, attribute, session)
        return session

    @staticmethod
    def align(frame):
        h, w = frame.shape[:2]
        if abs(w / h - 16 / 9) > .035:
            raise ValueError('Expected a 16:9 client screenshot')
        return cv2.resize(frame, (1280, 720), interpolation=cv2.INTER_AREA)

    def read_text(self, image):
        if isinstance(image, (str, Path)):
            image = cv2.imdecode(np.frombuffer(Path(image).read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        if image is None or not image.size:
            raise ValueError('Empty OCR image')
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        h, w = image.shape[:2]
        width = max(320, int(np.ceil(48 * w / h)))
        if width > 3200:
            raise ValueError('OCR crop exceeds supported line aspect ratio')
        resized_width = min(width, int(np.ceil(48 * w / h)))
        tensor = np.zeros((1, 3, 48, width), np.float32)
        resized = cv2.resize(image, (resized_width, 48), interpolation=cv2.INTER_LINEAR)
        tensor[0, :, :, :resized_width] = (resized.transpose(2, 0, 1).astype(np.float32) / 255 - .5) / .5
        session = self.session('ocr')
        started = time.perf_counter()
        output = session.run(None, {session.get_inputs()[0].name: tensor})[0][0]
        if output.shape[1] != len(self.characters):
            raise ValueError('Medium OCR dictionary does not match output classes')
        ids = output.argmax(1)
        keep = np.ones(len(ids), bool)
        keep[1:] = ids[1:] != ids[:-1]
        keep &= ids != 0
        return {'text': ''.join(self.characters[int(i)] for i in ids[keep]),
                'score': float(output.max(1)[keep].mean()) if keep.any() else 0.,
                'elapsed_ms': (time.perf_counter() - started) * 1000,
                'backend': 'PP-OCRv6_medium_rec_cpu', 'score_type': 'uncalibrated_character_score'}

    def detect(self, image):
        """DB text boxes, with independent confidence and polygon expansion."""
        from frame_store import private_mask
        image = private_mask(image)
        h, w = image.shape[:2]
        scale = min(1., 1280 / max(h, w))
        rh, rw = max(32, round(h * scale / 32) * 32), max(32, round(w * scale / 32) * 32)
        tensor = cv2.resize(image, (rw, rh)).astype(np.float32) / 255
        tensor = (tensor - np.array([.485, .456, .406], np.float32)) / np.array([.229, .224, .225], np.float32)
        session = self.session('detector')
        prob = session.run(None, {session.get_inputs()[0].name: np.ascontiguousarray(tensor.transpose(2, 0, 1)[None])})[0][0, 0]
        contours, _ = cv2.findContours((prob > .2).astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        for contour in contours[:3000]:
            rect = cv2.minAreaRect(contour)
            if min(rect[1]) < 3:
                continue
            points = cv2.boxPoints(rect)
            x, y, bw, bh = cv2.boundingRect(points)
            x0, y0, x1, y1 = max(0, x), max(0, y), min(prob.shape[1], x + bw), min(prob.shape[0], y + bh)
            mask = np.zeros((y1-y0, x1-x0), np.uint8)
            cv2.fillPoly(mask, [(points - [x0, y0]).astype(np.int32)], 1)
            score = cv2.mean(prob[y0:y1, x0:x1], mask)[0]
            if score < .45:
                continue
            area, perimeter = cv2.contourArea(points), cv2.arcLength(points, True)
            if perimeter <= 0:
                continue
            offset = pyclipper.PyclipperOffset()
            offset.AddPath(points.astype(np.int32).tolist(), pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
            expanded = offset.Execute(area * 1.4 / perimeter)
            if len(expanded) != 1:
                continue
            box = cv2.boxPoints(cv2.minAreaRect(np.array(expanded[0], np.float32)))
            box[:, 0] = np.clip(box[:, 0] * w / prob.shape[1], 0, w-1)
            box[:, 1] = np.clip(box[:, 1] * h / prob.shape[0], 0, h-1)
            a, b = np.floor(box.min(0)).astype(int)
            c, d = np.ceil(box.max(0)).astype(int)
            if c-a >= 6 and d-b >= 6:
                boxes.append((a, b, c, d, score))
        return sorted(boxes, key=lambda b: (round(b[1] / 12), b[0]))

    def read_page(self, image, language='zh-Hans-CN'):
        from frame_store import private_mask
        image = private_mask(image)
        lines = []
        for a, b, c, d, det_score in self.detect(image):
            prediction = self.read_text(image[b:d, a:c])
            if prediction['score'] < .85 or not prediction['text'].strip():
                continue
            text = prediction['text']
            lines.append({'text': text, 'model_score': prediction['score'], 'detector_score': det_score,
                'words': [{'text': text, 'x': int(a), 'y': int(b), 'width': int(c-a), 'height': int(d-b)}]})
        h, w = image.shape[:2]
        return {'width': w, 'height': h, 'language': language, 'backend': 'PP-OCRv6_medium_det_rec_cpu', 'lines': lines}
