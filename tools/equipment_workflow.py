"""Bounded equipment execution, with intents before every irreversible action."""
from frame_store import next_path, report_write, persist_references
import time
from pathlib import Path
import cv2
from equipment_vision import EquipmentVision,read_inventory_count
from m00_recognize import read_image
from navigation_input import navigate
from return_navigation_vision import ReturnNavigationVision
from common_navigation import ensure_main_menu
from runtime_policy import network_wait_seconds
from game_window import check_requested_stop
from equipment_receipts import (image_receipt, same_item, exactly_one_selected,
                                exactly_one_confirmed, selected_slot)


def visible_cards(frame,vision,quality,badge=None):
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
                obs={'state':'equipment_detail',**self.vision.detail_flags(frame,doc)}
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

    def refine(self):
        obs,_,frame,_=self.capture();assert obs['state']=='equipment_grid'
        candidates=visible_cards(frame,self.vision,'UR',20)
        if not candidates:raise RuntimeError('当前可见装备未找到已读清的UR纯数字20分徽章，不选择24或加号徽章。')
        card=candidates[0]
        def same_card(o,f):return any(c['point']==card['point'] for c in visible_cards(f,self.vision,'UR',20))
        self.go(card['point'],'equipment_grid',lambda o:o['state']=='equipment_detail','open_refine_candidate',same_card)
        obs,_,frame,_=self.capture()
        if not obs.get('score20'):raise RuntimeError('装备详情的20分徽章未独立核对，不进入精炼。')
        self.go([907,490],'equipment_detail',lambda o:o['state']=='equipment_refine','open_refinement',lambda o,f:o.get('score20'))
        before,geo,_,path=self.capture();fresh,fresh_geo,_,fresh_path=self.capture()
        if before['state']!='equipment_refine' or fresh['state']!='equipment_refine' or geo!=fresh_geo:
            raise RuntimeError('精炼页不稳定，不提交。')
        for key in ('score','before','cost'):
            if before.get(key)!=fresh.get(key):raise RuntimeError('精炼成本或余额发生变化，不提交。')
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

    def decompose(self):
        self.filter_r();obs,grid_geo,frame,path=self.capture(inventory=True)
        quality='R';r_unavailable=False
        inventory=obs.get('inventory',{}).get('value')
        if inventory==0:
            # The counter changes with the active filter (909 total / 269 R /
            # 0 N in live evidence). Require the saved R-only filter AND an
            # explicit empty-grid anchor across two observations.
            proof=self.ledger.data.get('filter_evidence',{})
            fresh,fresh_geo,_,fresh_path=self.capture(inventory=True)
            if (proof.get('qualities')!=['R'] or proof.get('saved') is not True
                    or proof.get('worn_hidden') is not True
                    or obs.get('empty_filtered_grid') is not True
                    or fresh.get('state')!='equipment_grid'
                    or fresh.get('inventory',{}).get('value')!=0
                    or fresh.get('empty_filtered_grid') is not True
                    or fresh_geo!=grid_geo):
                raise RuntimeError('R空列表未连续确认或单选筛选缺少保存回执，不降级N。')
            self.ledger.data['r_unavailable_evidence']={'filtered_count':0,
                'images':[str(path),str(fresh_path)],'filter':proof,'empty_grid_verified':True}
            self.ledger.save()
            quality='N';r_unavailable=True
            self.filter_quality('N');obs,_,frame,path=self.capture(inventory=True)
            inventory=obs.get('inventory',{}).get('value')
            if type(inventory) is not int or inventory<=0:
                raise RuntimeError('R已确认为0，但N数量未读清或也为0，未分解。')
        self.go([1135,670],'equipment_grid',lambda o:o['state']=='equipment_decompose_selection','open_single_decompose')
        obs,_,selection_before,selection_before_path=self.capture()
        if obs.get('matches_reviewed_one_item_selection'):raise RuntimeError('分解模式已经有选中项，不追加选择。')
        selectable = visible_cards(selection_before,self.vision,quality)
        if not selectable:
            raise RuntimeError('分解列表未找到品质一致的可选项，未选择。')
        chosen = selectable[0]
        # The game may reorder items on entering decomposition. Choose directly
        # from that filtered list; only bind the item until this selection click.
        inspected_grid, inspected_path = selection_before, selection_before_path
        # Retain the bound item before navigation can evict its observation.
        image_receipt(inspected_path)
        self.go(chosen['point'],'equipment_decompose_selection',lambda o:o.get('matches_reviewed_one_item_selection') is True,'select_one_'+quality,
                lambda o,f:same_item(inspected_grid,f,chosen)
                    and any(c['point']==chosen['point'] for c in visible_cards(f,self.vision,quality)))
        obs,geo,frame,path=self.capture()
        if not exactly_one_selected(self.vision,frame) or not selected_slot(self.vision,frame,chosen):
            raise RuntimeError('未核对目标品质、选中边框和恰好1件，不打开确认。')
        self.ledger.submit_decompose({'card':{'quality':quality,'locked':False,'equipped':False,'eligibility_basis':'filtered_selectable_item'},
            'selected_qualities':[quality],'selected_count':1,'inventory_count':inventory,'r_unavailable':r_unavailable,
            'visual_receipt':{'card':chosen,'filter':image_receipt(self.ledger.data['filter_evidence']['image']),
                'basis':'filtered_selectable_item','inspected_grid':image_receipt(inspected_path),
                'before_selection':image_receipt(selection_before_path),'selection':image_receipt(path)}},path)
        self.go([1185,670],'equipment_decompose_selection',lambda o:o['state']=='equipment_decompose_confirmation','open_one_item_confirmation')
        obs,geo,frame,path=self.capture();fresh,fresh_geo,fresh_frame,fresh_path=self.capture()
        if (any(o.get('state') != 'equipment_decompose_confirmation' for o in (obs, fresh))
                or not all(exactly_one_confirmed(self.vision,f) for f in (frame, fresh_frame))
                or geo != fresh_geo):
            raise RuntimeError('单件分解确认没有连续读清，保留未决，不提交。')
        pending=self.ledger.data['pending'];pending['confirmation']={'selected_count':1,'image':str(fresh_path),
            'images':[image_receipt(path),image_receipt(fresh_path)],'geometry':fresh_geo,'submitted':True};self.ledger.save()
        self.consume([697,467],'decompose_one',fresh_geo)
        obs,_,frame,result_path=self.wait(lambda o,g,f,p:o['state']=='equipment_decompose_result')
        self.ledger.data['pending']['result_evidence']=str(result_path)
        self.ledger.data['pending']['result_image']=image_receipt(result_path);self.ledger.save()
        self.go([640,658],'equipment_decompose_result',lambda o:o['state']=='equipment_grid','close_decomposition_result')
        obs,_,frame,path=self.capture(inventory=True);count=obs.get('inventory',{}).get('value')
        self.ledger.data['pending']['return_grid_image']=image_receipt(path);self.ledger.save()
        if not self.ledger.settle_decompose(count,result_path,path):raise RuntimeError('分解单件图像回执不完整或数量矛盾，保留未决，不重复提交。')

    def initialize(self):
        self.go([1141,67],'equipment_grid',lambda o:o['state']=='equipment_filter','open_filter_for_initialization')
        obs,geo,frame,path=self.capture()
        expected=[self.ledger.data.get('selected_quality')]
        if expected not in (['R'],['N']) or obs.get('selected_qualities')!=expected:raise RuntimeError('初始化前不是已核对的品质单选。')
        fresh,fresh_geo,_,fresh_path=self.capture()
        if fresh_geo!=geo or fresh.get('selected_qualities')!=expected or fresh.get('worn_hidden') is not True:
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
