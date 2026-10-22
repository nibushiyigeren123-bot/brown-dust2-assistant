"""Shared CPU runtime for general base and game-specific local visual models."""
import json
import os
os.environ['OPENBLAS_NUM_THREADS']='1'
from pathlib import Path
import time
import cv2
import numpy as np
import onnxruntime as ort
from PIL import Image

class GameVision:
    def __init__(self,bundle,threads=2):
        self.bundle=Path(bundle);self.threads=threads;self._ocr=None;self._visual=None
        self.characters=['blank']+(self.bundle/'dictionary.txt').read_text(encoding='utf-8').splitlines()+[' ']
    def session(self,name):
        attribute='_ocr' if name=='ocr' else '_visual'
        session=getattr(self,attribute)
        if session is None:
            options=ort.SessionOptions();options.intra_op_num_threads=self.threads;options.inter_op_num_threads=1
            options.execution_mode=ort.ExecutionMode.ORT_SEQUENTIAL;options.log_severity_level=3
            session=ort.InferenceSession(str(self.bundle/f'{name}.onnx'),sess_options=options,providers=['CPUExecutionProvider'])
            setattr(self,attribute,session)
        return session
    @staticmethod
    def align(frame):
        h,w=frame.shape[:2]
        if abs(w/h-16/9)>.035:raise ValueError('Expected a client-area screenshot close to 16:9')
        return cv2.resize(frame,(1280,720),interpolation=cv2.INTER_AREA)
    def read_text(self,image):
        if isinstance(image,(str,Path)):image=cv2.imdecode(np.fromfile(image,np.uint8),cv2.IMREAD_COLOR)
        if image is None or not image.size:raise ValueError('Empty OCR input')
        if image.ndim==2:image=cv2.cvtColor(image,cv2.COLOR_GRAY2BGR)
        h,w=image.shape[:2];width=max(320,int(48*w/h));rw=min(width,int(np.ceil(48*w/h)))
        scaled=cv2.resize(image,(rw,48),interpolation=cv2.INTER_LINEAR)
        tensor=np.zeros((1,3,48,width),np.float32)
        tensor[0,:,:,:rw]=(scaled.transpose(2,0,1).astype(np.float32)/255-.5)/.5
        session=self.session('ocr');started=time.perf_counter()
        output=session.run(None,{session.get_inputs()[0].name:tensor})[0][0]
        ids=output.argmax(1);keep=np.ones(len(ids),bool);keep[1:]=ids[1:]!=ids[:-1];keep&=ids!=0
        text=''.join(self.characters[int(i)] for i in ids[keep])
        score=float(output.max(1)[keep].mean()) if keep.any() else 0.0
        return {'text':text,'score':score,'elapsed_ms':(time.perf_counter()-started)*1000,
                'backend':'onnx_cpu','score_type':'uncalibrated_character_score'}
    def read_region(self,frame,box):
        a,b,c,d=map(int,box);return self.read_text(self.align(frame)[b:d,a:c])
    def image_vector(self,image):
        from frame_store import private_mask
        if isinstance(image,(str,Path)):image=Image.open(image).convert('RGB')
        elif isinstance(image,np.ndarray):image=Image.fromarray(cv2.cvtColor(image,cv2.COLOR_BGR2RGB))
        image=Image.fromarray(private_mask(np.asarray(image.convert('RGB'))));scale=min(120/image.width,120/image.height)
        image=image.resize((max(1,round(image.width*scale)),max(1,round(image.height*scale))),Image.Resampling.BILINEAR)
        canvas=Image.new('RGB',(128,128),(0,0,0));canvas.paste(image,((128-image.width)//2,(128-image.height)//2))
        array=np.asarray(canvas).astype(np.float32)/255
        array=(array-np.array([.485,.456,.406],np.float32))/np.array([.229,.224,.225],np.float32)
        tensor=np.ascontiguousarray(array.transpose(2,0,1)[None])
        session=self.session('visual');vector=session.run(None,{session.get_inputs()[0].name:tensor})[0][0]
        return vector/np.maximum(np.linalg.norm(vector),1e-12)
    def match(self,image,references,minimum=.9):
        query=self.image_vector(image)
        scores=sorted(((name,float(query@vector)) for name,vector in references.items()),key=lambda item:item[1],reverse=True)
        return {'match':scores[0][0] if scores and scores[0][1]>=minimum else None,
                'scores':scores[:3],'score_type':'cosine_similarity_not_probability'}
