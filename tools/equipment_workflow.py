"""Bounded equipment execution, with intents before every irreversible action."""
from frame_store import next_path, report_write, persist_references
import time
from pathlib import Path
import cv2
from equipment_vision import EquipmentVision,read_inventory_count,read_plain_badge
from m00_recognize import read_image
from navigation_input import navigate
from return_navigation_vision import ReturnNavigationVision
from common_navigation import ensure_main_menu
from runtime_policy import network_wait_seconds
from game_window import check_requested_stop
from equipment_receipts import (image_receipt, same_item, exactly_one_selected,
                                exactly_one_confirmed, selected_slot)


def visible_cards(frame,vision,quality,badge=None,diagnostics=None):
    if 'quality_'+quality not in vision.images:
        raise RuntimeError(quality+'品质字形尚未实机校准，保留筛选证据，不选择或分解装备。')
    image=cv2.resize(frame,(1280,720),interpolation=cv2.INTER_AREA)
    def match(template,box):
        a,b,c,d=box
        return float(cv2.matchTemplate(image[b:d,a:c],template,cv2.TM_CCOEFF_NORMED).max())
    cards=[]
    for row in range(6):
        for col in range(8):
            x,y=480+84*col,97+84*row
            q=match(vision.images['quality_'+quality],(x+48,y-2,x+82,y+25))
            plain=vision.images.get('quality_'+quality+'_plain')
            if plain is not None:
                q=max(q,match(plain,(x+48,y-2,x+82,y+25)))
            score=match(vision.images['badge_'+str(badge)],(x+46,y+36,x+82,y+71)) if badge is not None else None
            # [证据] 失败时保留每个格子的真实品质/徽章分数，不降低选择阈值。
            if diagnostics is not None:
                diagnostics.append({'row':row,'column':col,'quality_score':q,'badge_score':score,
                    'accepted':q>=.93 and (score is None or score>=.94),
                    'rejection':'quality_below_threshold' if q<.93 else 'badge_below_threshold' if score is not None and score<.94 else None})
            if q>=.93 and (score is None or score>=.94):
                cards.append({'quality':quality,'badge':badge,'point':[x+40,y+40],'row':row,'column':col,
                              'quality_score':q,'badge_score':score})
    return cards


class EquipmentRun:
    def __init__(self,window,ledger,report,output,save):
        self.window,self.ledger,self.report=window,ledger,report
        self.output,self.save=Path(output),save
        self.vision,self.returns=EquipmentVision(),ReturnNavigationVision()
        self.timeout=network_wait_seconds()
        self.index=0

    def capture(self, *, inventory=False):
        check_requested_stop()
        image,geo=self.window.capture();path=self.output/f'equipment_{self.index:03d}.png';self.index+=1
        path=next_path(path);image.save(path);frame=read_image(path)
        obs=self.vision.inspect_file(path,self.output/'ocr',frame=frame,include_inventory=inventory)
        if obs['state']=='unknown':
            returned=self.returns.inspect(frame)
            if returned and returned['state']=='equipment_detail':
                from daily_vision import OcrDocument,windows_ocr
                doc=OcrDocument(windows_ocr(path,self.output/'ocr'))
                obs={'state':'equipment_detail',**self.vision.detail_flags(frame,doc,self.output/'badge_ocr')}
            else:
                from daily_vision import OcrDocument,windows_ocr
                doc=OcrDocument(windows_ocr(path,self.output/'ocr'))
                if doc.find('消耗品',(150,10,400,65)):
                    obs={'state':'equipment_bag'}
        self.report['observations'].append({'image':str(path),'geometry':geo,'observation':obs})
        self.report['progress']=self.ledger.progress.state.copy();self.report['pending']=self.ledger.data['pending'];self.save()
        return obs,geo,frame,path

    def go(self,point,source,accept,label,validate=None):
        def capture():
            obs,geo,frame,path=self.capture()
            return obs,geo,path
        def action(event,path,attempt):
            self.report['actions'].append({'type':label,'image':str(path),'attempt':attempt,**event});self.save()
        return navigate(self.window,capture,point,source,accept,self.timeout,action,
                        validate=(lambda o,g,p:validate(o,read_image(p))) if validate is not None else None)

    def wait(self,predicate):
        deadline=time.monotonic()+self.timeout
        while time.monotonic()<deadline:
            captured=self.capture()
            if predicate(*captured):return captured
            time.sleep(.3)
        raise RuntimeError('装备页面或结果未在等待时限内读清，保留记录，不重发提交。')

    def consume(self,point,label,geo):
        # The caller has persisted its one-shot intent before reaching this API.
        self.report['pending']=self.ledger.data['pending'];self.save()
        event=self.window.click_client([point[0]*geo['width']/1280,point[1]*geo['height']/720],geo,press_ms=250)
        self.report['actions'].append({'type':label,'consumes':True,**event})
        self.ledger.data['pending']['input_receipt']=event;self.ledger.save();self.save()
        time.sleep(.8)

    def grid(self):
        obs,geo,frame,path=self.capture()
        if obs['state']=='equipment_grid':return
        menu=ensure_main_menu(self.window,self.output/'start_navigation',True,self.timeout)
        if not menu['completed']:raise RuntimeError('装备开始前未确认主页。')
        # The bag opens its most recently used tab. The equipment tab is fixed.
        self.go([247,655],'main_menu',lambda o:o['state'] in ('equipment_grid','equipment_bag'),'open_bag')
        # One transient OCR miss after navigation is not evidence of a wrong
        # page. Require two known bag/grid frames before selecting a category.
        previous = None
        stable = 0
        def known_bag(obs, geo, frame, path):
            nonlocal previous, stable
            signature = (obs.get('state'), geo)
            if obs.get('state') not in ('equipment_grid', 'equipment_bag'):
                previous, stable = None, 0
                return False
            stable = stable + 1 if signature == previous else 1
            previous = signature
            return stable >= 2
        obs,_,_,_ = self.wait(known_bag)
        if obs['state'] == 'equipment_bag':
            self.go([116,166], 'equipment_bag', lambda o:o['state']=='equipment_grid', 'select_equipment_tab')

    def refine_candidates(self,frame,scores=None,deadline=None):
        """[功能] UR字形独立确认后读完整徽章；候选分数须遵循既有1..23纯数字规则。"""
        result=[]
        # [品质] 原模板仍做有界定位；真实UR小文字两尺度OCR补证，不能全局降低原选择阈值。
        normalized=cv2.resize(frame,(1280,720),interpolation=cv2.INTER_AREA)
        from daily_vision import windows_ocr,compact
        import hashlib
        import numpy as np
        for row in range(6):
            for col in range(8):
                check_requested_stop()
                if deadline is not None and time.monotonic()>=deadline:break
                x,y=480+84*col,97+84*row
                crop=normalized[y-2:y+25,x+48:x+82]
                template=self.vision.images['quality_UR']
                score=float(cv2.matchTemplate(crop,template,cv2.TM_CCOEFF_NORMED).max())
                readings=[]
                visible=float(np.percentile(cv2.cvtColor(crop,cv2.COLOR_BGR2GRAY),90))>=110
                if score>=.65 and visible:
                    for scale in (1,2):
                        raw=cv2.imencode('.png',cv2.copyMakeBorder(cv2.resize(crop,None,fx=scale,fy=scale),4,4,4,4,cv2.BORDER_CONSTANT,value=(0,0,0)))[1]
                        path=self.output/'badge_ocr'/('quality_'+hashlib.sha256(raw.tobytes()).hexdigest()[:20]+'.png');path.parent.mkdir(parents=True,exist_ok=True)
                        from frame_store import write_encoded_png
                        if not path.exists():write_encoded_png(path,raw)
                        payload=windows_ocr(path,self.output/'badge_ocr/ocr',language='en-US')
                        readings.append(compact(''.join(line['text'] for line in payload['lines'])))
                evidence={'row':row,'column':col,'quality_score':score,'quality_readings':readings,'quality_foreground_visible':visible}
                if scores is not None:scores.append(evidence)
                if readings==['UR','UR']:
                    # [顺序] 每个格子独立品质→完整徽章；找到首个合法候选即返回，避免扫描所有UR拖长等待。
                    badge=read_plain_badge(frame,(x+46,y+36,x+82,y+71),self.output/'badge_ocr','grid_badge')
                    evidence['badge_evidence']=badge
                    if badge['value'] is not None:
                        return [{'quality':'UR','row':row,'column':col,'point':[x+40,y+40],
                                 'quality_score':score,'quality_evidence':evidence,
                                 'badge':badge['value'],'badge_evidence':badge}]
        return result

    def wait_refine_candidate(self):
        """[功能] 等待卡片实际加载并连续两帧认出同一卡；超时保存原网格和分数。"""
        deadline=time.monotonic()+self.timeout
        previous=None
        history=[]
        while time.monotonic()<deadline:
            check_requested_stop()
            obs,geo,frame,path=self.capture()
            scores=[]
            candidates=self.refine_candidates(frame,scores,deadline) if obs['state']=='equipment_grid' else []
            signature=(geo,[(c['row'],c['column'],c['badge']) for c in candidates]) if candidates else None
            history.append({'image':str(path),'state':obs['state'],'candidates':candidates,'scores':scores})
            history=history[-4:]
            if signature is not None and signature==previous:
                self.report['refine_candidate_wait']={'status':'verified','frames':history}
                persist_references([str(path),history[-2]['image']])
                self.save()
                return candidates[0]
            previous=signature
            # [停止] 短间隔回到捕获前检查F8/停止文件，不把等待实现成阻塞长睡眠。
            time.sleep(.3)
        self.report['refine_candidate_wait']={'status':'timeout','timeout_seconds':self.timeout,'frames':history}
        persist_references([row['image'] for row in history])
        self.save()
        raise RuntimeError('装备网格在等待时限内仍未连续确认同一UR纯数字1至23分徽章；已保留原图与各格分数，不选择24或加号徽章。')

    def refine(self):
        # [流程] 页标题出现不代表卡片已加载；候选稳定后仍由详情与提交前双帧独立核验。
        card=self.wait_refine_candidate()
        def same_card(o,f):return any(c['point']==card['point'] and c['badge']==card['badge'] for c in self.refine_candidates(f))
        self.go(card['point'],'equipment_grid',lambda o:o['state']=='equipment_detail','open_refine_candidate',same_card)
        obs,_,frame,_=self.capture()
        if obs.get('quality')!='UR' or obs.get('score')!=card['badge']:raise RuntimeError('装备详情完整分数与网格候选不一致，不进入精炼。')
        self.go([907,490],'equipment_detail',lambda o:o['state']=='equipment_refine','open_refinement',lambda o,f:o.get('quality')=='UR' and o.get('score')==card['badge'])
        before,geo,_,path=self.capture();fresh,fresh_geo,_,fresh_path=self.capture()
        if before['state']!='equipment_refine' or fresh['state']!='equipment_refine' or geo!=fresh_geo:
            raise RuntimeError('精炼页不稳定，不提交。')
        for key in ('score','before','cost'):
            if before.get(key)!=fresh.get(key):raise RuntimeError('精炼成本或余额发生变化，不提交。')
        # [提交门禁] 网格、详情、精炼页必须为同一完整纯数字分数，成本余额仍由当前页面读取。
        if fresh.get('score')!=card['badge']:
            raise RuntimeError('精炼页完整分数与所选装备不一致，不提交。')
        self.ledger.submit_refine(fresh,fresh_path)
        self.consume(fresh['points']['submit'],'refine_once',fresh_geo)
        self.wait(lambda o,g,f,p:o['state']=='equipment_refine' and self.ledger.settle_refine(o,p))
        self.go([113,33],'equipment_refine',lambda o:o['state']=='equipment_grid','return_from_refinement')

    def filter_r(self):
        return self.filter_quality('R')

    def filter_quality(self, quality):
        if quality not in ('R','N'):
            raise RuntimeError('分解仅允许R或已核实无R后的N单选。')
        self.go([1141,67],'equipment_grid',lambda o:o['state']=='equipment_filter','open_equipment_filter')
        obs,_,frame,_=self.capture()
        changed=False
        points={'N':[353,404],'R':[431,404]}
        selected=obs.get('selected_qualities')
        if selected not in ([],['R'],['N']):
            raise RuntimeError('原品质筛选未读清，不进行N/R双选。')
        if selected and selected!=[quality]:
            self.go(points[selected[0]],'equipment_filter',lambda o:o.get('selected_qualities')==[],
                    'clear_previous_single_quality',lambda o,f:o.get('selected_qualities')==selected)
            changed=True
            selected=[]
        if selected==[]:
            self.go(points[quality],'equipment_filter',lambda o:o.get('selected_qualities')==[quality],'select_'+quality+'_only',
                    lambda o,f:o.get('selected_qualities')==[])
            changed=True
        obs,_,frame,_=self.capture()
        if not obs.get('worn_hidden'):
            self.go([606,523],'equipment_filter',lambda o:o.get('worn_hidden') is True,'hide_equipped_items',
                    lambda o,f:self.vision.anchor(f,'filter_worn_default',.97))
            changed=True
        if changed:
            self.go([917,540],'equipment_filter',lambda o:o['state']=='equipment_grid','save_'+quality+'_filter',
                    lambda o,f:o.get('selected_qualities')==[quality] and o.get('worn_hidden') is True)
        else:
            # Save is disabled when the persisted filter already matches.
            self.go([973,160],'equipment_filter',lambda o:o['state']=='equipment_grid','close_unchanged_'+quality+'_filter')
        self.go([1141,67],'equipment_grid',lambda o:o['state']=='equipment_filter','verify_saved_'+quality+'_filter')
        obs,_,_,path=self.capture()
        if obs.get('selected_qualities')!=[quality] or obs.get('worn_hidden') is not True:
            raise RuntimeError('重新打开后未确认品质单选和隐藏已穿戴。')
        self.ledger.record_filter([quality],path,saved=True,worn_hidden=True)
        self.go([973,160],'equipment_filter',lambda o:o['state']=='equipment_grid','close_saved_filter')

    def filter_for_detail(self,quality):
        """[筛选应用] 保持原穿戴显示设置；资格由详情逐件核对，不重开筛选。"""
        if quality not in ('R','N'):raise RuntimeError('仅支持R优先、已核实无R后的N。')
        self.go([1141,67],'equipment_grid',lambda o:o['state']=='equipment_filter','open_detail_filter')
        obs,_,_,_=self.capture();selected=obs.get('selected_qualities');changed=False
        points={'N':[353,404],'R':[431,404]}
        if selected not in ([],['R'],['N']):raise RuntimeError('原单品质筛选未读清，不更改。')
        if selected and selected!=[quality]:
            self.go(points[selected[0]],'equipment_filter',lambda o:o.get('selected_qualities')==[],
                    'clear_previous_detail_quality',lambda o,f:o.get('selected_qualities')==selected)
            selected=[];changed=True
        if not selected:
            self.go(points[quality],'equipment_filter',lambda o:o.get('selected_qualities')==[quality],
                    'select_detail_'+quality,lambda o,f:o.get('selected_qualities')==[])
            changed=True
        first,geo,_,first_path=self.capture();fresh,fresh_geo,_,fresh_path=self.capture()
        if (geo!=fresh_geo or any(o.get('state')!='equipment_filter' or o.get('selected_qualities')!=[quality]
                                 for o in (first,fresh))):
            raise RuntimeError('应用筛选前未连续确认单品质，未点击确认。')
        image_receipt(first_path);image_receipt(fresh_path)
        self.go([917,540] if changed else [973,160],'equipment_filter',lambda o:o['state']=='equipment_grid',
                'confirm_detail_filter' if changed else 'close_unchanged_detail_filter',
                lambda o,f:o.get('selected_qualities')==[quality])
        obs,grid_geo,_,grid_path=self.capture(inventory=True)
        if obs['state']!='equipment_grid':raise RuntimeError('应用筛选后普通列表未确认。')
        self.ledger.record_detail_filter([quality],fresh_path,grid_path,grid_geo)

    def decompose(self):
        """[单件路线] 普通R列表→详情剪刀→确认1件，禁止进入一键分解选择模式。"""
        self.filter_for_detail('R');obs,grid_geo,frame,path=self.capture(inventory=True)
        quality='R';r_unavailable=False;inventory=obs.get('inventory',{}).get('value')
        if inventory==0:
            proof=self.ledger.data.get('detail_filter_evidence',{})
            fresh,fresh_geo,_,fresh_path=self.capture(inventory=True)
            if (proof.get('qualities')!=['R'] or proof.get('basis')!='detail_filter_applied'
                    or obs.get('empty_filtered_grid') is not True or fresh.get('state')!='equipment_grid'
                    or fresh.get('inventory',{}).get('value')!=0 or fresh.get('empty_filtered_grid') is not True
                    or fresh_geo!=grid_geo):
                raise RuntimeError('R空列表与单品质应用证据不足，不降级N。')
            self.ledger.data['r_unavailable_evidence']={'filtered_count':0,'images':[image_receipt(path),image_receipt(fresh_path)],
                'filter':proof,'empty_grid_verified':True};self.ledger.save()
            quality='N';r_unavailable=True;self.filter_for_detail('N')
            obs,grid_geo,frame,path=self.capture(inventory=True);inventory=obs.get('inventory',{}).get('value')
            if type(inventory) is not int or inventory<=0:raise RuntimeError('N数量未读清或为0，不分解。')
        # [普通列表稳定] 两次实帧同一格独立读品质，不能沿用批量选择模式坐标状态。
        deadline=time.monotonic()+self.timeout;previous=None;chosen=None
        while time.monotonic()<deadline:
            check_requested_stop();obs,grid_geo,frame,path=self.capture(inventory=True);cells=[]
            cards=visible_cards(frame,self.vision,quality,diagnostics=cells) if obs['state']=='equipment_grid' else []
            self.report.setdefault('decompose_candidate_diagnostics',[]).append({'page':obs,'quality':quality,
                'cells':cells,'candidate_count':len(cards),'image':image_receipt(path)})
            self.report['decompose_candidate_diagnostics']=self.report['decompose_candidate_diagnostics'][-4:];self.save()
            signature=(grid_geo,[(c['row'],c['column']) for c in cards]) if cards else None
            if signature is not None and signature==previous:chosen=cards[0];break
            previous=signature;time.sleep(.3)
        if chosen is None:raise RuntimeError('普通列表未连续确认目标品质候选，未点击一键分解。')
        inspected=frame;inspected_path=path;image_receipt(path)
        self.go(chosen['point'],'equipment_grid',lambda o:o['state']=='equipment_detail','open_decompose_detail',
                lambda o,f:same_item(inspected,f,chosen) and any(c['point']==chosen['point'] for c in visible_cards(f,self.vision,quality)))
        opened=[a for a in self.report['actions'] if a.get('type')=='open_decompose_detail' and a.get('input_kind')=='left_click']
        if not opened:raise RuntimeError('缺少普通列表进入详情的真实输入回执。')
        opening=opened[-1];input_grid=image_receipt(opening['image'])
        # [详情资格] 未锁、未穿戴、品质一致和剪刀两帧稳定；不复用UR精炼数字规则。
        detail,dgeo,_,dpath=self.capture();fresh,fgeo,_,fpath=self.capture()
        def eligible(o):
            return (o.get('state')=='equipment_detail' and o.get('quality')==quality and o.get('unlocked') is True
                    and o.get('not_equipped') is True and o.get('scissors_verified') is True and o.get('detail_close_verified') is True)
        if (dgeo!=fgeo or not all(eligible(o) for o in (detail,fresh))
                or not detail.get('item_title') or detail['item_title']!=fresh.get('item_title')):
            raise RuntimeError('详情品质、锁定、穿戴或同一装备身份未核实，未打开分解确认。')
        details=[image_receipt(dpath),image_receipt(fpath)]
        self.go(fresh['points']['decompose'],'equipment_detail',lambda o:o['state']=='equipment_decompose_confirmation',
                'open_detail_single_confirmation',lambda o,f:eligible(o) and o.get('item_title')==detail['item_title'])
        scissors=[a for a in self.report['actions'] if a.get('type')=='open_detail_single_confirmation' and a.get('input_kind')=='left_click']
        if not scissors:raise RuntimeError('缺少详情剪刀输入回执，未提交分解。')
        obs,geo,frame,path=self.capture();fresh,fresh_geo,fresh_frame,fresh_path=self.capture()
        if (geo!=fresh_geo or any(o.get('state')!='equipment_decompose_confirmation' for o in (obs,fresh))
                or not all(exactly_one_confirmed(self.vision,f) for f in (frame,fresh_frame))):
            raise RuntimeError('详情单件确认未连续核实恰好1件，未创建消费意图。')
        confirmations=[image_receipt(path),image_receipt(fresh_path)]
        observation={'card':{'quality':quality,'locked':False,'equipped':False,'eligibility_basis':'detail_single_item'},
            'selected_qualities':[quality],'selected_count':1,'inventory_count':inventory,'r_unavailable':r_unavailable,
            'visual_receipt':{'basis':'detail_single_item','card':chosen,'filter':self.ledger.data['detail_filter_evidence'],
                'inspected_grid':image_receipt(inspected_path),'input_grid':input_grid,'geometry':grid_geo,
                'open_detail_input':opening,'details':details,'confirmations':confirmations,
                'open_confirmation_input':scissors[-1],'input_detail':image_receipt(scissors[-1]['image']),
                'r_unavailable_evidence':self.ledger.data.get('r_unavailable_evidence') if quality=='N' else None}}
        # [消费边界] 剪刀只打开确认；最终确认前才写一次intent，之后无结果必须保留pending。
        self.ledger.submit_decompose(observation,fresh_path)
        pending=self.ledger.data['pending'];pending['confirmation']={'selected_count':1,'image':str(fresh_path),
            'images':confirmations,'geometry':fresh_geo,'submitted':False};self.ledger.save()
        # [输入前重核] 原图核验/OCR可能耗时，intent落盘后再读当前确认；变化时保留未决而不点击。
        final,final_geo,final_frame,final_path=self.capture()
        if (final_geo!=fresh_geo or final.get('state')!='equipment_decompose_confirmation'
                or not exactly_one_confirmed(self.vision,final_frame)):
            raise RuntimeError('intent已保存但确认页发生变化，保留pending，未发送最终确认。')
        pending['pre_input_confirmation']=image_receipt(final_path);self.ledger.save()
        self.consume([697,467],'decompose_one',final_geo)
        pending['confirmation']['submitted']=True;self.ledger.save()
        obs,_,frame,result_path=self.wait(lambda o,g,f,p:o['state']=='equipment_decompose_result')
        self.ledger.data['pending']['result_evidence']=str(result_path)
        self.ledger.data['pending']['result_image']=image_receipt(result_path);self.ledger.save()
        self.go([640,658],'equipment_decompose_result',lambda o:o['state']=='equipment_grid','close_decomposition_result')
        obs,_,frame,path=self.capture(inventory=True);count=obs.get('inventory',{}).get('value')
        self.ledger.data['pending']['return_grid_image']=image_receipt(path);self.ledger.save()
        if not self.ledger.settle_decompose(count,result_path,path):
            raise RuntimeError('详情单件分解回执链不完整或数量矛盾，保留pending，不重复确认。')

    def initialize(self):
        self.go([1141,67],'equipment_grid',lambda o:o['state']=='equipment_filter','open_filter_for_initialization')
        obs,geo,frame,path=self.capture()
        expected=[self.ledger.data.get('selected_quality')]
        if expected not in (['R'],['N']) or obs.get('selected_qualities')!=expected:raise RuntimeError('初始化前不是已核对的品质单选。')
        fresh,fresh_geo,_,fresh_path=self.capture()
        # [新筛选兼容] 详情路线从未承诺隐藏穿戴；初始化前仅旧basis维持隐藏前提。
        detail_filter=self.ledger.data.get('detail_filter_evidence',{}).get('basis')=='detail_filter_applied'
        if (fresh_geo!=geo or fresh.get('selected_qualities')!=expected
                or (not detail_filter and fresh.get('worn_hidden') is not True)):
            raise RuntimeError('初始化前筛选状态变化，未提交初始化。')
        geo,path=fresh_geo,fresh_path
        self.ledger.data['pending']={'module':'equipment','stage':'initialize','evidence':str(path),'actions':[{'type':'initialize','status':'intent'}]};self.ledger.save()
        self.window.click_client([703*geo['width']/1280,540*geo['height']/720],geo,press_ms=250)
        self.report['actions'].append({'type':'initialize_filter_once'});self.save()
        self.wait(lambda o,g,f,p:o['state']=='equipment_filter' and o.get('default_visible_options') is True)
        self.go([917,540],'equipment_filter',lambda o:o['state']=='equipment_grid','save_initialized_filter',
                lambda o,f:o.get('default_visible_options') is True)
        self.go([1141,67],'equipment_grid',lambda o:o['state']=='equipment_filter','verify_initialized_filter')
        obs,_,_,path=self.capture()
        if obs.get('default_visible_options') is not True:raise RuntimeError('保存后未确认筛选默认值。')
        self.go([973,160],'equipment_filter',lambda o:o['state']=='equipment_grid','close_initialized_filter')
        self.ledger.progress.initialize_filter()
        self.ledger.data['events'].append({'submission':self.ledger.data['pending'],'evidence':str(path)})
        self.ledger.data.update(pending=None,selected_quality=None);self.ledger.save()

    def execute(self):
        if self.ledger.data['pending']:raise RuntimeError('装备有未决操作，先对账，不重放。')
        self.grid()
        if not self.ledger.progress.state['refine_completed']:self.refine()
        if not self.ledger.progress.state['decompose_completed']:self.decompose()
        if not self.ledger.progress.state['filter_initialized']:self.initialize()
        returned=ensure_main_menu(self.window,self.output/'return_to_menu',True,self.timeout)
        self.report.update(completed=returned['completed'],returned_to_main_menu=returned['completed'],
                           progress=self.ledger.progress.state.copy(),pending=self.ledger.data['pending'],
                           scope='one refinement, one verified R, one filter initialization')
        self.save()
