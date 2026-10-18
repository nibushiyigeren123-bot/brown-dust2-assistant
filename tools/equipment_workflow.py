"""Bounded equipment execution, with intents before every irreversible action."""
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


def visible_cards(frame,vision,quality,badge=None):
    image=cv2.resize(frame,(1280,720),interpolation=cv2.INTER_AREA)
    def match(template,box):
        a,b,c,d=box
        return float(cv2.matchTemplate(image[b:d,a:c],template,cv2.TM_CCOEFF_NORMED).max())
    cards=[]
    for row in range(6):
        for col in range(8):
            x,y=480+84*col,97+84*row
            q=match(vision.images['quality_'+quality],(x+48,y-2,x+82,y+25))
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

    def capture(self):
        check_requested_stop()
        image,geo=self.window.capture();path=self.output/f'equipment_{self.index:03d}.png';self.index+=1
        image.save(path);frame=read_image(path)
        obs=self.vision.inspect_file(path,self.output/'ocr')
        if obs['state']=='unknown':
            returned=self.returns.inspect(frame)
            if returned and returned['state']=='equipment_detail':
                obs={'state':'equipment_detail','unlocked':self.vision.anchor(frame,'detail_unlocked',.97),
                     'not_equipped':self.vision.anchor(frame,'detail_wear_available',.97),
                     'score20':self.vision.anchor(frame,'detail_score20',.97)}
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
            if obs['state']==source and not accept(obs) and validate is not None and not validate(obs,frame):
                raise RuntimeError('装备导航目标重新核对未通过：'+label)
            return obs,geo,path
        def action(event,path,attempt):
            self.report['actions'].append({'type':label,'image':str(path),'attempt':attempt,**event});self.save()
        return navigate(self.window,capture,point,source,accept,self.timeout,action)

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
        obs,_,frame,_=self.capture()
        if obs['state']!='equipment_grid':
            from daily_vision import OcrDocument,windows_ocr
            doc=OcrDocument(windows_ocr(self.report['observations'][-1]['image'],self.output/'bag_ocr'))
            if not doc.find('消耗品',(150,10,400,65)):
                raise RuntimeError('未核对到背包消耗品页，不猜测装备分类。')
            self.go([116,166],obs['state'],lambda o:o['state']=='equipment_grid','select_equipment_tab')

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
        self.go([1141,67],'equipment_grid',lambda o:o['state']=='equipment_filter','open_equipment_filter')
        obs,_,frame,_=self.capture()
        if obs.get('selected_qualities')==[]:
            self.go([431,404],'equipment_filter',lambda o:o.get('selected_qualities')==['R'],'select_R_only',
                    lambda o,f:o.get('selected_qualities')==[])
        elif obs.get('selected_qualities')!=['R']:
            raise RuntimeError('原品质筛选未读清，未切换为N/R双选。')
        obs,_,frame,_=self.capture()
        if not obs.get('worn_hidden'):
            self.go([606,523],'equipment_filter',lambda o:o.get('worn_hidden') is True,'hide_equipped_items',
                    lambda o,f:self.vision.anchor(f,'filter_worn_default',.97))
        self.go([917,540],'equipment_filter',lambda o:o['state']=='equipment_grid','save_R_filter',
                lambda o,f:o.get('selected_qualities')==['R'] and o.get('worn_hidden') is True)
        self.go([1141,67],'equipment_grid',lambda o:o['state']=='equipment_filter','verify_saved_R_filter')
        obs,_,_,path=self.capture()
        if obs.get('selected_qualities')!=['R'] or obs.get('worn_hidden') is not True:
            raise RuntimeError('重新打开后未确认R单选和隐藏已穿戴。')
        self.ledger.record_filter(['R'],path,saved=True,worn_hidden=True)
        self.go([973,160],'equipment_filter',lambda o:o['state']=='equipment_grid','close_saved_filter')

    def decompose(self):
        self.filter_r();obs,_,frame,_=self.capture()
        inventory=obs.get('inventory',{}).get('value')
        if type(inventory) is not int:raise RuntimeError('R装备总数未读清，不分解。')
        candidates=visible_cards(frame,self.vision,'R');chosen=None
        for card in candidates:
            def same_card(o,f):return any(c['point']==card['point'] for c in visible_cards(f,self.vision,'R'))
            self.go(card['point'],'equipment_grid',lambda o:o['state'] in ('equipment_detail','equipment_reviewed_R_detail'),'inspect_R_candidate',same_card)
            obs,_,frame,path=self.capture()
            if obs.get('unlocked') and obs.get('not_equipped'):
                chosen={**card,'detail_evidence':str(path)}
            self.go([946,217],obs['state'],lambda o:o['state']=='equipment_grid','close_R_detail')
            if chosen:break
        if not chosen:raise RuntimeError('当前可见R未发现已核对的未锁且未穿戴项；尚未查完R，不启用N后备。')
        self.go([1135,670],'equipment_grid',lambda o:o['state']=='equipment_decompose_selection','open_single_decompose')
        obs,_,_,_=self.capture()
        if obs.get('matches_reviewed_one_item_selection'):raise RuntimeError('分解模式已经有选中项，不追加选择。')
        self.go(chosen['point'],'equipment_decompose_selection',lambda o:o.get('matches_reviewed_one_item_selection') is True,'select_one_R')
        obs,geo,frame,path=self.capture()
        if not self.vision.anchor(frame,'selected_count_one',.97):raise RuntimeError('未核对恰好选中1件，不打开确认。')
        self.ledger.submit_decompose({'card':{'quality':'R','locked':False,'equipped':False,'detail_evidence':chosen['detail_evidence']},
            'selected_qualities':['R'],'selected_count':1,'inventory_count':inventory},path)
        self.go([1185,670],'equipment_decompose_selection',lambda o:o['state']=='equipment_decompose_confirmation','open_one_item_confirmation')
        obs,geo,frame,path=self.capture();fresh,fresh_geo,fresh_frame,fresh_path=self.capture()
        if (any(o.get('state') != 'equipment_decompose_confirmation' for o in (obs, fresh))
                or not all(self.vision.anchor(f, name, .97) for f in (frame, fresh_frame)
                           for name in ('decompose_confirm_warning', 'decompose_confirm_one', 'decompose_confirm_button'))
                or geo != fresh_geo):
            raise RuntimeError('单件分解确认没有连续读清，保留未决，不提交。')
        pending=self.ledger.data['pending'];pending['confirmation']={'selected_count':1,'image':str(fresh_path),'submitted':True};self.ledger.save()
        self.consume([697,467],'decompose_one',fresh_geo)
        obs,_,frame,result_path=self.wait(lambda o,g,f,p:o['state']=='equipment_decompose_result')
        self.ledger.data['pending']['result_evidence']=str(result_path);self.ledger.save()
        self.go([640,658],'equipment_decompose_result',lambda o:o['state']=='equipment_grid','close_decomposition_result')
        obs,_,frame,path=self.capture();count=obs.get('inventory',{}).get('value')
        if not self.ledger.settle_decompose(count,result_path,path):raise RuntimeError('装备数量尚未确认只减少1件，保留未决。')

    def initialize(self):
        self.go([1141,67],'equipment_grid',lambda o:o['state']=='equipment_filter','open_filter_for_initialization')
        obs,geo,frame,path=self.capture()
        if obs.get('selected_qualities')!=['R']:raise RuntimeError('初始化前不是已核对的R单选。')
        fresh,fresh_geo,_,fresh_path=self.capture()
        if fresh_geo!=geo or fresh.get('selected_qualities')!=['R'] or fresh.get('worn_hidden') is not True:
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
