"""[活动入口] 只认主页右下活动卡字幕区域的“解锁”，活动身份留到中心独立确认。"""
import hashlib
from pathlib import Path
import cv2
import numpy as np
from daily_vision import OcrDocument,windows_ocr,compact
from frame_store import write_encoded_png

UNLOCK_ROI=(1020,535,1230,572)
ENTRY_POINT=[1120,520]


def unlock_entry(image,texts):
    """[限定字幕] 前缀与logo均不参与；暗字幕拒绝，仅返回非消费的卡片入口。"""
    h,w=image.shape[:2]
    if abs(w/h-16/9)>.025:return None
    normalized=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
    a,b,c,d=UNLOCK_ROI;gray=cv2.cvtColor(normalized[b:d,a:c],cv2.COLOR_BGR2GRAY)
    if float(np.percentile(gray,99))<175 or not texts or not all('解锁' in compact(text) for text in texts):return None
    return {'state':'main_menu','points':{'enter_event':ENTRY_POINT.copy()},
            'event_entry_evidence':{'recognition':'fixed_carousel_unlock_keyword','keyword':'解锁',
                'roi':list(UNLOCK_ROI),'readings':texts,'identity_deferred_until_event_hub':True}}


def unlock_from_payload(image,payload):
    doc=OcrDocument(payload)
    rows=[row['text'] for row in doc.within(UNLOCK_ROI) if '解锁' in row['text']]
    return unlock_entry(image,rows) if len(rows)==1 else None


def read_unlock_entry(image,output):
    """[裁图优先] 字幕裁片放大两份，只要求两份都读到解锁，不比较前缀或浮点字框。"""
    normalized=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
    a,b,c,d=UNLOCK_ROI;crop=normalized[b:d,a:c];texts=[]
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    for scale in (3,4):
        encoded=cv2.imencode('.png',cv2.resize(crop,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC))[1]
        path=output/f'event_unlock_{hashlib.sha256(encoded.tobytes()).hexdigest()[:18]}.png'
        write_encoded_png(path,encoded)
        payload=windows_ocr(path,output/'ocr')
        texts.append(compact(''.join(row['text'] for row in payload['lines'])))
    return unlock_entry(image,texts)
