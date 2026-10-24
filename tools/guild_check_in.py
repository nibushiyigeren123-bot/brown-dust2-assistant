"""Guild entry and check-in receipt. Default is inspection; --execute is explicit."""
from frame_store import next_path, report_write, persist_references
import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from local_cache import cache_path, load_local_config

from daily_tasks import guard_foreign_pending, guild_pending_phase, PendingConflictError
from event_reward_rules import load_config
from m00_recognize import ROOT
from pending_daily import exclusive_ledger
from phase08_vision import Phase08Vision


class GuildController:
    def __init__(self, data, *, recover_pending=False):
        """[输入] recover_pending仅由调用端校验账本后显式启用；默认兼容旧行为。"""
        self.data=data
        self.dismissed=False
        self.back_clicked=False
        self.dismiss_attempts=0
        self.back_attempts=0
        self.review_reason=None
        self.entered_this_run=False
        self.guild_stable_since=None
        self.guild_stable_frames=0
        self.no_receipt_ready=False
        # [状态] 恢复开关与连续主页观察只保存在本次控制器，不改历史账本身份。
        self.recover_pending=recover_pending
        self.home_stable_since=None
        self.home_stable_frames=0
        self.home_ready=False

    def observe(self, obs, now):
        # [分支] 旧pending恢复先连续确认主页，单帧主页不能结算或触发重入。
        if obs['state'] == 'main_menu':
            if self.home_stable_since is None:
                self.home_stable_since=now
            self.home_stable_frames+=1
            self.home_ready=self.home_stable_frames >= 2 and now-self.home_stable_since >= 1
        else:
            # [分支] 未知/加载/其他页会中断连续确认，之后必须重新计时计帧。
            self.home_stable_since=None
            self.home_stable_frames=0
            self.home_ready=False
        # Only uninterrupted recognized guild pages count. Recheck before input.
        if obs['state'] != 'guild_page':
            self.guild_stable_since=None
            self.guild_stable_frames=0
            self.no_receipt_ready=False
            return
        if self.guild_stable_since is None:
            self.guild_stable_since=now
        self.guild_stable_frames+=1
        stable=self.guild_stable_frames >= 3 and now-self.guild_stable_since >= 10
        # [分支] 旧entry也可等待十秒无收据后返回；旧return已进入返回阶段，无需重等。
        self.no_receipt_ready=stable and (self.entered_this_run or self.recover_pending)
        if (self.recover_pending and self.data.get('pending', {}).get('phase') == 'no_receipt_return_pending'):
            self.no_receipt_ready=True
        if stable and not self.entered_this_run and not self.recover_pending and not self.data.get('issued'):
            self.request_review_return('existing_guild_pending_without_receipt')

    def request_review_return(self, reason):
        self.review_reason=reason

    def step(self, obs):
        state=obs['state']
        if state=='guild_check_in_receipt':
            if self.dismiss_attempts>=3:
                return {'status':'needs_review','reason':'guild_receipt_did_not_close_after_three_attempts'}
            return {'status':'action','action':'dismiss_receipt','point':obs['points']['dismiss']}
        if state=='main_menu':
            if self.recover_pending and not self.home_ready:
                return {'status':'wait','reason':'confirming_main_menu_for_guild_recovery'}
            if self.data.get('issued'):
                return {'status':'finished','reason':'check_in_receipt_and_menu_verified'}
            if (self.data.get('completed') and not self.data.get('pending') and self.data.get('outcome') == 'visited_without_reward_receipt') or (
                    ((self.entered_this_run and self.back_clicked) or self.recover_pending) and
                    self.data.get('pending', {}).get('phase') == 'no_receipt_return_pending'):
                # [输出] 只有已返回阶段且确认主页才结束，未弹奖励不能写issued=True。
                return {'status':'finished','reason':'visited_without_reward_receipt'}
            if self.review_reason:
                return {'status':'needs_review','reason':self.review_reason}
            if self.data.get('pending'):
                if (self.recover_pending and not self.entered_this_run
                        and self.data['pending']['phase'] == 'entry_attempted'):
                    # [状态] 只补一次旧入场；commit后entered_this_run永久阻止本次重发。
                    if obs.get('guild_entry'):
                        return {'status':'action','action':'enter_guild','point':obs['guild_entry']['point']}
                    return {'status':'needs_review','reason':'guild_icon_not_recognized'}
                return {'status':'wait','reason':'waiting_for_guild_entry_transition'}
            if obs.get('guild_entry'):
                return {'status':'action','action':'enter_guild','point':obs['guild_entry']['point']}
            return {'status':'needs_review','reason':'guild_icon_not_recognized'}
        if state=='guild_page':
            if self.data.get('issued') or self.review_reason or self.no_receipt_ready:
                if self.back_attempts<3:
                    return {'status':'action','action':'return_to_menu','point':obs['points']['back']}
                return {'status':'needs_review','reason':'guild_return_did_not_reach_menu_after_three_attempts'}
            # A bare page alone is not a reward receipt; observe a bounded stable visit.
            return {'status':'wait','reason':'waiting_for_check_in_receipt'}
        return {'status':'wait','reason':'unrecognized_or_loading'}

    def commit(self,decision):
        action=decision['action']
        if action=='enter_guild':
            self.entered_this_run=True
            self.data['pending']={'module':'guild','phase':'entry_attempted'}
        elif action=='dismiss_receipt':
            self.data['issued']=True
            self.data['pending']={'module':'guild','phase':'receipt_seen_return_pending'}
            self.dismissed=True
            self.dismiss_attempts+=1
        elif action=='return_to_menu':
            if self.no_receipt_ready and not self.data.get('issued') and not self.review_reason:
                self.data['pending']={'module':'guild','phase':'no_receipt_return_pending'}
            self.back_clicked=True
            self.back_attempts+=1


def save_json(path,data):
    from frame_store import active
    if active() and path.name == 'run.json':
        return report_write(path, data)
    persist_references(data)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    with temporary.open('w',encoding='utf-8',newline='\n') as handle:
        handle.write(json.dumps(data,ensure_ascii=False,indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary,path)


def run(args):
    config=load_config(args.config)
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=True)
    report={'execute':args.execute,'completed':False,'actions':[],'observations':[],
            'cache_date':config['cache_date']}
    from event_reward_rules import guild_disabled_reason
    reason=guild_disabled_reason(config, ['guild'], args.execute)
    if reason:
        report['stopped']=reason
        save_json(output/'run.json',report)
        return report
    vision=Phase08Vision()
    if args.image:
        if args.execute:
            raise ValueError('An image inspection cannot execute')
        report['observation']=vision.inspect_file(args.image,output)
        save_json(output/'inspection.json',report)
        return report
    from game_window import GameWindow
    import win32api
    window=GameWindow.find()
    ledger=cache_path(ROOT, config, 'guild')
    def save():
        save_json(output/'run.json',report)
    with exclusive_ledger(ROOT/'runtime/game_control'):
        if args.execute:
            guard_foreign_pending(ROOT/'runtime',ledger, module='guild')
        data=json.loads(ledger.read_text(encoding='utf-8')) if args.execute and ledger.exists() else {
            'schema_version':1,'cache_date':config['cache_date'],
            'pending':None,'issued':False,'completed':False}
        if data['cache_date'] != config['cache_date']:
            raise RuntimeError('Guild ledger identity mismatch')
        recover_pending=False
        if args.execute and data.get('pending') is not None:
            # [校验] 在持有执行锁时独立复核当前游戏日、路径与完整协议；不信任CLI授权。
            from game_calendar import game_day
            phase=guild_pending_phase(data, game_day().isoformat(), path=ledger, directory=ROOT/'runtime')
            if phase is None:
                raise PendingConflictError('Guild pending identity invalid; retain the original ledger for review')
            recover_pending=getattr(args, 'recover_pending', False)
        controller=GuildController(data, recover_pending=recover_pending)
        from runtime_policy import network_wait_seconds
        network_grace = network_wait_seconds() if args.execute else 8
        deadline=time.monotonic()+max(args.timeout, network_grace * 2)
        entry_transition_since=None
        try:
            while time.monotonic()<deadline:
                if win32api.GetAsyncKeyState(0x77)&0x8000:
                    report['stopped']='F8'
                    break
                image,geometry=window.capture()
                path=output/f"frame_{len(report['observations']):03}.png"
                path=next_path(path);image.save(path)
                obs=vision.inspect_file(path,output)
                controller.observe(obs, time.monotonic())
                decision=controller.step(obs)
                report['observations'].append({'image':str(path),'observation':obs,'decision':decision})
                save()
                if not args.execute:
                    break
                if decision['status']=='finished':
                    # [状态] 无收据只记访问并返回；issued仍为False，不冒充奖励到账。
                    data['pending']=None
                    data['completed']=True
                    data['outcome']=decision['reason']
                    report['outcome']=decision['reason']
                    save_json(ledger,data)
                    report['completed']=True
                    report['returned_to_main_menu']=True
                    break
                if decision['status']=='needs_review':
                    report['returned_to_main_menu']=obs['state']=='main_menu'
                    report['stopped']=decision
                    break
                if decision['status']=='action':
                    # A fast auto-issued banner can appear during OCR. Re-read
                    # the UI before committing either an entry or a return.
                    fresh,fresh_geometry=window.capture()
                    fresh_path=output/f"preclick_{len(report['observations']):03}.png"
                    fresh_path=next_path(fresh_path);fresh.save(fresh_path)
                    current=vision.inspect_file(fresh_path,output)
                    controller.observe(current, time.monotonic())
                    candidate=controller.step(current)
                    if candidate!=decision:
                        continue
                    if win32api.GetAsyncKeyState(0x77)&0x8000:
                        report['stopped']='F8'
                        break
                    controller.commit(decision)
                    # Entry itself can issue the reward: persist before clicking.
                    save_json(ledger,data)
                    x,y=decision['point']
                    receipt=window.click_client([x*fresh_geometry['width']/1280,y*fresh_geometry['height']/720],fresh_geometry)
                    report['actions'].append({**decision,**receipt})
                    save()
                    time.sleep(0.9)
                if obs['state']=='main_menu' and data.get('pending') and not data.get('issued') and controller.entered_this_run:
                    # [异常] 本次入场后一直停主页，网络宽限到期转needs_review，不再重入或死等。
                    entry_transition_since=entry_transition_since or time.monotonic()
                    if time.monotonic()-entry_transition_since>network_grace:
                        controller.request_review_return('entry_was_attempted_but_no_check_in_receipt')
                else:
                    entry_transition_since=None
                time.sleep(0.25)
            if args.execute and not report['completed'] and 'stopped' not in report:
                report['stopped']='timeout_without_verified_check_in'
        except Exception as error:
            report['error']=str(error)
            raise
        finally:
            report['progress']=data
            save()
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=ROOT/'config/event_and_rewards.json')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--image',type=Path)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--recover-pending',action='store_true',help='Recover a validated current-day guild pending from the visible page')
    parser.add_argument('--timeout',type=float,default=40)
    args=parser.parse_args()
    result=run(args)
    print(json.dumps({k:result.get(k) for k in ('execute','completed','stopped','observation')},ensure_ascii=False))
    if args.execute and not result['completed']:
        raise SystemExit(1)


if __name__=='__main__':
    main()
