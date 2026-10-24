"""通用活动标题与战斗页面识别；观察本身不授予资源消费权限。"""
import hashlib
import re
import json
from functools import lru_cache
import cv2
import numpy as np
from project_paths import ROOT
from daily_vision import OcrDocument
from event_ap_vision import resource_balance


@lru_cache(maxsize=1)
def _cleared_markers():
    folder=ROOT/'assets/event_battle'
    spec=json.loads((folder/'cleared_stage.json').read_text(encoding='utf-8'))
    return [(item,cv2.imdecode(np.fromfile(str(folder/item['file']),np.uint8),cv2.IMREAD_COLOR))
            for item in spec['markers']]


def cleared_stage_evidence(image, observation):
    """[通关证据] 当前准备页标题绑定关卡，三项真实勾选同时存在；灰按钮和AP不参与。"""
    if observation.get('state')!='event_preparation' or type(observation.get('stage')) is not int:
        return None
    normalized=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
    scores=[]
    for item,reference in _cleared_markers():
        a,b,c,d=item['box'];patch=normalized[b:d,a:c]
        score=float(cv2.matchTemplate(patch,reference,cv2.TM_CCOEFF_NORMED)[0,0])
        mask=cv2.cvtColor(reference,cv2.COLOR_BGR2GRAY)>=220
        brightness=float(cv2.cvtColor(patch,cv2.COLOR_BGR2GRAY)[mask].mean())
        if not np.isfinite(score) or score<.94 or brightness<190:return None
        scores.append(round(score,4))
    return {'kind':'current_stage_three_claimed_reward_checks','event_id':observation.get('event_id'),
            'mode':observation.get('mode'),'stage':observation['stage'],'scores':scores}


def title_identity(doc, roi):
    """[身份] 只从活动标题区域的实际OCR文字组成身份，不套用上一期名称。"""
    rows=[]
    ignored={'OFF','ON','MIN','MAX','NEW','DAY','DDAY','SPECIAL','SKIN','SKINS'}
    for row in doc.within(roi):
        text=re.sub(r'[^A-Z0-9\u4e00-\u9fff]','',row['text'].upper())
        if (len(text)<3 or text in ignored or re.fullmatch(r'D\d+|\d+',text)
                or any(word in text for word in ('战斗','奖励','活动','推荐','当前','商店','服装','还剩','解锁'))):
            continue
        if re.fullmatch(r'[A-Z]{3,24}|[\u4e00-\u9fff]{3,20}',text):
            rows.append((row['box'][1],text,row['box']))
    rows.sort()
    words=[text for _,text,_ in rows]
    if not words:return None
    title=' '.join(words)
    # [兼容] 旧活动只有实际标题正证据才能映射原账本ID；其他标题一律生成独立身份。
    identity='Loyal_Maid_20261016' if title.replace(' ','')=='LOYALMAID' else 'title_'+hashlib.sha256(title.replace(' ','').encode('utf-8')).hexdigest()[:20]
    return {'event_id':identity,
            'event_title':title,'title_verified':True,'title_source':'current_page_OCR',
            'title_boxes':[box for _,_,box in rows]}


def generic_event_observation(image, payload, base_state=None):
    """[页面] 语义控件先确认活动页面，再核对本页标题；资源只按右上固定X/5核对。"""
    doc=OcrDocument(payload)
    if base_state=='main_menu':
        from event_entry_vision import unlock_from_payload
        return unlock_from_payload(image,payload)
    normal=doc.find('普通战斗',(300,600,510,690),exact=True)
    challenge=doc.find('挑战战斗',(490,600,650,690),exact=True)
    if normal and challenge:
        title=title_identity(doc,(700,100,1250,470))
        if title:
            return {'state':'event_hub',**title,**resource_balance(doc),'points':{'normal':normal['point'],'challenge':challenge['point'],'back':[113,33]}}
    # [资源] 活动名称和图标可变；准备页与结果页共用固定位置的AP数字合同。
    stage=[]
    for row in doc.within((530,60,800,125)):
        match=re.fullmatch(r'(普通|挑战)战斗(\d+)',row['text'])
        if match:stage.append({'mode':'normal' if match[1]=='普通' else 'challenge','stage':int(match[2])})
    if len(stage)==1 and doc.find('通关奖励',(430,130,600,200)) and doc.find('反复奖励',(1030,65,1220,125)):
        title=title_identity(doc,(10,580,350,710))
        if not title:return None
        balance=resource_balance(doc)
        costs=[]
        for row in doc.within((600,620,1200,710)):
            match=re.fullmatch(r'战斗(\d+)',row['text'])
            if match:costs.append(int(match[1]))
        # [未知] 页面身份成立时保留未知余额；控制器仍须读清AP才可消费或结算。
        result={'state':'event_preparation',**title,**stage[0],**balance,
                    'resource_kind':'event_ap',
                    'unit_cost':costs[0] if len(costs)==1 else None,
                    'points':{'back':[113,33],**({'open_auto':doc.find('OFF',(1080,620,1240,675),exact=True)['point']} if doc.find('自动战斗',(1080,665,1240,710),exact=True) and doc.find('OFF',(1080,620,1240,675),exact=True) else {})}}
        evidence=cleared_stage_evidence(image,result)
        if evidence:result['cleared_stage_evidence']=evidence
        return result
    return None
