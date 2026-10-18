"""Navigation-only daily hunting status probe. Never claims or starts a battle."""
from types import SimpleNamespace
from pathlib import Path
from task_rewards import TaskRewardRun, save
from daily_vision import compact

TASK_NAMES = {'在狩猎场或冒险航线中进行战斗', '在狩猎场或冒陰航线中进行战斗'}

def hunting_task_evidence(observation):
    if observation.get('state') != 'daily_tasks':
        return None
    matches = [row for row in observation.get('rows', []) if compact(row.get('name', '')) in TASK_NAMES]
    if len(matches) != 1:
        return None
    row = matches[0]
    if type(row.get('current')) is not int or row.get('target') != 1 or row['current'] not in (0, 1):
        return None
    return {'status': 'done' if row['current'] == 1 else 'not_done', 'row': row}

class HuntingTaskProbe(TaskRewardRun):
    def _capture_once(self):
        result=super()._capture_once()
        callback=getattr(self,'on_frame',None)
        if callback:callback(result[0],result[3])
        return result

    def click(self, point, state, label, claim=False):
        if claim or label not in ('open_daily_tasks', 'select_daily', 'return_to_main_menu'):
            raise RuntimeError('狩猎任务核对只允许打开、滚动和返回，不允许领奖或战斗。')
        from navigation_input import navigate
        from runtime_policy import network_wait_seconds
        target='main_menu' if label=='return_to_main_menu' else 'daily_tasks'
        def capture():
            obs,geo,_,path=self._capture_once()
            return obs,geo,path
        def record(receipt,path,attempt):
            self.report['actions'].append({'type':label,'state':state,'evidence':str(path),'attempt':attempt,**receipt})
            save(self.args.output/'run.json',self.report)
        navigate(self.window,capture,point,state,lambda obs:obs['state']==target,network_wait_seconds(self.data_root),record)

    def scan_all(self):
        """Initial menu preflight: record task rows without claiming any rewards."""
        from reward_endpoints import RewardEndpoints
        from datetime import datetime, timezone
        from uuid import uuid4
        import cv2
        endpoints = RewardEndpoints()
        self.enter('daily')
        self.scroll('daily', 6)
        rows = {}; hunting = None; previous_view=None; stationary=0
        for index in range(13):
            observation, _, image, path = self.capture()
            fresh, _, second_image, second = self.capture()
            if observation.get('state') != 'daily_tasks' or fresh.get('state') != 'daily_tasks':
                raise RuntimeError('开头核对任务时画面仍在变化，未沿用不稳定进度。')
            # OCR of unrelated task names can fluctuate with animated backgrounds.
            # Only rows independently agreeing in both captures become evidence.
            stable_rows=[row for row in observation.get('rows',[]) if row in fresh.get('rows',[])]
            for row in stable_rows:
                if row['name'] in rows and rows[row['name']] != row:
                    raise RuntimeError('任务列表相同名称出现不同进度，需要核对。')
                rows[row['name']] = row
            evidence = hunting_task_evidence(observation)
            if evidence and evidence==hunting_task_evidence(fresh):
                evidence.update(image=str(path),verified_image=str(second),cache_date=self.config['cache_date'])
                hunting = evidence
            view=cv2.resize(second_image,(1280,720))[85:645,450:950]
            stationary=stationary+1 if previous_view is not None and float(cv2.absdiff(previous_view,view).mean())<.8 else 0
            previous_view=view
            at_bottom=endpoints.confirmed(image,'daily:bottom') and endpoints.confirmed(second_image,'daily:bottom')
            if at_bottom or stationary>=2:
                if not rows or (self.config['hunting']['enabled'] and not hunting):
                    raise RuntimeError('任务列表未读清，或未找到每日狩猎进度。')
                self.click([113,32],'daily_tasks','return_to_main_menu')
                if self.capture()[0]['state'] != 'main_menu':
                    raise RuntimeError('开头任务核对后尚未确认返回主页。')
                audit={'cache_date':self.config['cache_date'],'audit_id':uuid4().hex,
                       'observed_at':datetime.now(timezone.utc).isoformat(),'rows':list(rows.values()),
                       'hunting':hunting,'completed':True,'source':'initial_menu_task_scan',
                       'coverage':'observed_rows_only','bottom_template_verified':at_bottom}
                if hunting:hunting['audit_id']=audit['audit_id']
                self.report.update(completed=True,task_audit=audit,returned_to_main_menu=True)
                save(self.args.output/'run.json',self.report)
                return audit
            self.scroll('daily',-3)
        raise RuntimeError('未确认每日任务列表底部，停止本轮初始化检查。')

    def scan(self):
        self.enter('daily')
        previous = None
        # Search a bounded set of views. Absence is never treated as completion.
        for index in range(13):
            observation, _, _, path = self.capture()
            evidence = hunting_task_evidence(observation)
            if evidence:
                fresh, _, _, second = self.capture()
                if hunting_task_evidence(fresh) == evidence:
                    evidence.update(image=str(path), verified_image=str(second), cache_date=self.config['cache_date'])
                    self.click([113, 32], 'daily_tasks', 'return_to_main_menu')
                    home, *_ = self.capture()
                    if home['state'] != 'main_menu':
                        raise RuntimeError('狩猎任务已读清，但尚未确认返回主菜单。')
                    self.report.update(completed=True, task_evidence=evidence, returned_to_main_menu=True)
                    save(self.args.output / 'run.json', self.report)
                    return evidence
            if index == 0:
                self.scroll('daily', 6)
            elif index < 12:
                self.scroll('daily', -3)
        raise RuntimeError('未找到可连续核对的每日狩猎0/1或1/1，不猜测状态，不提交狩猎。')

def probe_hunting_task(config, output, data_root, on_frame=None):
    from game_calendar import check_execution_day
    check_execution_day(config['cache_date'])
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    from game_window import GameWindow
    from daily_vision import DailyVision, inspect_frame
    window,vision = GameWindow.find(),DailyVision()
    for index in range(4):
        image,geo=window.capture();path=output/f'start_{index}.png';image.save(path)
        observed=inspect_frame(path,vision,config,output/'start_ocr')
        if observed['state'] not in ('hunt_map','hunt_dialog'):break
        fresh,fresh_geo=window.capture();fresh_path=output/f'start_{index}_verified.png';fresh.save(fresh_path)
        verified=inspect_frame(fresh_path,vision,config,output/'start_ocr')
        if geo!=fresh_geo or verified['state']!=observed['state']:continue
        point=verified['points']['cancel'] if verified['state']=='hunt_dialog' else verified['back_point']
        window.click_client([point[0]*geo['width']/1280,point[1]*geo['height']/720],geo)
        import time;time.sleep(.8)
    probe = HuntingTaskProbe(SimpleNamespace(output=output, data_root=data_root), config, {}, output / 'unused.json')
    probe.on_frame=on_frame
    try:
        return probe.scan()
    except Exception as error:
        probe.report['error'] = str(error)
        save(output / 'run.json', probe.report)
        raise


def initial_task_audit(config, output, data_root):
    """Caller owns the game lock. Persist only observed progress, never identity."""
    from daily_tasks import DailyLedger
    from local_cache import cache_path
    from game_calendar import check_execution_day
    check_execution_day(config['cache_date'])
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    probe=HuntingTaskProbe(SimpleNamespace(output=output,data_root=data_root),config,{},output/'unused.json')
    try:
        # User requested that visiting daily tasks also claims current daily AND
        # weekly rewards. The normal final reward sweep still checks again.
        from task_rewards import ledger_path,begin_sweep,TaskRewardRun
        reward_path=ledger_path(Path(data_root),config)
        import json
        rewards=json.loads(reward_path.read_text(encoding='utf-8')) if reward_path.exists() else {
            'schema_version':1,'cache_date':config['cache_date'],'pending':None}
        begin_sweep(rewards)
        reward_args=SimpleNamespace(output=output/'reward_precheck',data_root=Path(data_root))
        reward_args.output.mkdir(parents=True,exist_ok=True)
        TaskRewardRun(reward_args,config,rewards,reward_path).execute()
        audit=probe.scan_all()
        audit['initial_daily_weekly_rewards_checked']=True
        ledger=DailyLedger(cache_path(data_root,config),config)
        if ledger.data['pending']:raise RuntimeError('有未决动作，不能用任务核对覆盖回执。')
        evidence=audit.get('hunting')
        if evidence:
            progress=ledger.data['hunting']
            if (progress['daily_done'] or progress['runs'].get('daily',0)) and evidence['status']=='not_done':
                raise RuntimeError('每日0/1与本地狩猎回执冲突，不重复执行。')
            progress['daily_check']=evidence
            if evidence['status']=='done':progress['daily_done']=True
            ledger.save()
        save(Path(data_root)/'runtime/daily_task_audit.json',audit)
        return audit
    except Exception as error:
        probe.report.update(completed=False,error=str(error))
        save(output/'run.json',probe.report)
        raise
